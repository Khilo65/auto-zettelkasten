"""Read-only full-note recall with optional, strictly local semantic search.

Search never generates notes, downloads models, calls a provider or changes the
mapping graph. Missing or stale semantic material leaves full-note word search
available, with explicit coverage rather than a silently reduced corpus.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
from numbers import Real
from pathlib import Path
import re
import struct
import threading
import shutil
import time
from typing import Any, Mapping

from .documents import DocumentError, DocumentIndexChanged, DocumentService
from .hybrid_search import HybridSearchIndex
from .mapping_text import CLEANING_VERSION, prepare_embedding_spans, prepare_note_embedding_spans


_MAX_CACHE_BYTES = 128 * 1024 * 1024
_DEFAULT_OVERRIDES = Path.home() / ".config/auto-zettelkasten/workspace-overrides"
_RECALL_SCHEMA = "full-note-recall-v1"


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _digest(value):
    return _sha(_json(value))


def _stamp(path):
    info = path.stat()
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _safe_path(root, relative):
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Recall cache paths must remain inside the workspace.")
    path = root.joinpath(relative)
    if not path.resolve().is_relative_to(root):
        raise ValueError("Recall cache paths must remain inside the workspace.")
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("Recall cache paths cannot contain symbolic links.")
    return path


class _CacheReader:
    def __init__(self, root):
        self.root, self.bytes, self.stamps = root, 0, {}

    def read(self, relative, maximum):
        path = _safe_path(self.root, relative)
        before = path.stat()
        if not path.is_file() or before.st_nlink != 1 or before.st_size > maximum:
            raise ValueError("Recall cache file exceeds its resource or link limit.")
        self.bytes += before.st_size
        if self.bytes > _MAX_CACHE_BYTES:
            raise ValueError("Recall cache exceeds the 128 MiB read limit.")
        stamp = _stamp(path)
        with path.open("rb") as handle:
            raw = handle.read(maximum + 1)
        if len(raw) != before.st_size or _stamp(path) != stamp:
            raise ValueError("Recall cache changed during read.")
        self.stamps[str(path)] = stamp
        return raw

    def object(self, relative, maximum):
        def invalid(_):
            raise ValueError("Nonfinite JSON in recall cache.")
        value = json.loads(self.read(relative, maximum), parse_constant=invalid)
        if not isinstance(value, dict):
            raise ValueError("Recall cache records must be objects.")
        return value

    def verify(self):
        return all(_stamp(Path(path)) == stamp for path, stamp in self.stamps.items())


def _unit(values):
    values = list(values)
    if (not 1 <= len(values) <= 4096 or any(isinstance(v, bool) or not isinstance(v, Real)
                                         or not math.isfinite(v) for v in values)):
        raise ValueError("Recall vectors must contain finite real values.")
    norm = math.sqrt(sum(float(v) ** 2 for v in values))
    if norm <= 1e-12:
        raise ValueError("Recall vector has zero norm.")
    return [float(v) / norm for v in values]


def _semantic_fingerprint(text):
    # Blank separators around an inserted managed block are presentation. Keep
    # substantive characters and indentation intact, including code examples.
    return _sha(re.sub(r"\n(?:[ \t]*\n)+", "\n\n", text.replace("\r\n", "\n")).strip())


def _corpus(snapshot, *, instance_id=None, library_id=None, annotation_snapshots=None):
    candidates, raw_by_source, excluded, paths = [], {}, Counter(), set()
    for document in snapshot["documents"]:
        if document["path"] in paths:
            excluded["duplicate_path"] += 1
            continue
        paths.add(document["path"])
        fields = document["frontmatter"]
        scope = document["source_coverage"]["status"]
        if document["navigation"]:
            excluded["navigation"] += 1
            continue
        if scope == "metadata_only":
            excluded["metadata_only"] += 1
            continue
        identity = fields.get("identity") if isinstance(fields.get("identity"), Mapping) else {}
        if any(requested is not None and key in container and str(container[key]) != requested
               for container in (fields, identity)
               for key, requested in (("instance_id", instance_id), ("library_id", library_id))):
            excluded["different_source_scope"] += 1
            continue
        raw = document["text"]
        receipt = (annotation_snapshots or {}).get(document["path"], {})
        annotation_hash = receipt.get("block_hash")
        clean = prepare_note_embedding_spans(raw, annotation_hash=annotation_hash) if annotation_hash else prepare_embedding_spans(raw)
        text = "".join(span["text"] for span in clean["spans"])
        annotations = []
        if annotation_hash:
            from .zotero_annotations import annotation_recall_records, verified_annotation_span
            if verified_annotation_span(raw, annotation_hash):
                try:
                    annotations = annotation_recall_records(receipt.get("groups", []))
                except (ValueError, TypeError, KeyError):
                    # A broken receipt cannot hide authored text or confer
                    # source provenance on an invalid annotation snapshot.
                    clean = prepare_embedding_spans(raw)
                    text = "".join(span["text"] for span in clean["spans"])
        if not text.strip():
            excluded["empty_substantive_text"] += 1
            continue
        supplied = fields.get("source_id")
        sid = supplied if isinstance(supplied, str) and 0 < len(supplied) <= 256 else "note-" + _sha(document["path"])
        candidates.append((sid, document, text, clean, annotations))
    counts = Counter(row[0] for row in candidates)
    rows = []
    for sid, document, text, clean, annotations in candidates:
        fields = document["frontmatter"]
        if counts[sid] > 1:
            sid = "note-" + _sha(document["path"])
        title = document.get("personal_display_title") or fields.get("title")
        title = title[:1000] if isinstance(title, str) and title.strip() else Path(document["path"]).stem[:1000]
        semantic_text = text
        annotation_ranges = []
        for record in annotations:
            quoted = record["quote"].strip()
            comment = record["comment"].strip()
            if not quoted and not comment:
                continue
            start = len(text)
            text += "\n\n" + ("Source quotation: " + quoted if quoted else "")
            text += ("\nPersonal comment: " + comment) if comment else ""
            annotation_ranges.append({"start": start, "end": len(text), "record": record})
        row = {"source_id": sid, "title": title, "text": text, "content_sha256": _sha(text),
               "note_path": document["path"], "coverage": {**document["source_coverage"],
                   "input_representation": "full_note", "cleaning_version": CLEANING_VERSION,
                   "note_content_sha256": _sha(raw), "excerpt_basis":
                   "source_summary_and_confirmed_annotations" if annotations else "cleaned_note_text"}}
        nid = fields.get("note_id")
        if isinstance(nid, str) and 0 < len(nid) <= 256:
            row["note_id"] = nid
        rows.append(row)
        raw_by_source[sid] = {"text": document["text"], "sha256": _sha(document["text"]),
                              "clean": clean, "path": document["path"],
                              "semantic_sha256": _semantic_fingerprint(semantic_text),
                              "source_content_sha256": _sha(semantic_text),
                              "annotation_ranges": annotation_ranges}
        baseline = document.get("semantic_baseline_text")
        if isinstance(baseline, str):
            baseline_clean = prepare_embedding_spans(baseline)
            baseline_text = "".join(span["text"] for span in baseline_clean["spans"])
            if _semantic_fingerprint(baseline_text) == raw_by_source[sid]["semantic_sha256"]:
                raw_by_source[sid]["mapping_baseline"] = {"text": baseline, "sha256": _sha(baseline),
                                                        "clean": baseline_clean, "path": document["path"]}
    return rows, raw_by_source, dict(excluded)


def _snapshot_identity(snapshot):
    return [(row["path"], row["revision"]) for row in snapshot["documents"]]


def _annotation_pointers(result, raw_sources, max_chars):
    """Attach locators only for confirmed annotation passages in the excerpt."""
    while True:
        for note in result["notes"]:
            note.pop("annotation_pointers", None)
            refs = []
            for entry in raw_sources[note["source_id"]].get("annotation_ranges", []):
                if entry["start"] < note["excerpt_end"] and entry["end"] > note["excerpt_start"]:
                    record = entry["record"]
                    refs.append({key: record[key] for key in ("scope", "attachment_key", "annotation_key",
                                "page_index", "page_label", "uri", "type", "version")})
            if refs:
                note["annotation_pointers"] = refs
        paths = {note["note_path"] for note in result["notes"]}
        result["relationships"] = [row for row in result["relationships"]
                                   if row["source"] in paths and row["target"] in paths]
        size = len(_json({key: result[key] for key in ("notes", "relationships")}))
        if size <= max_chars:
            result["chars"] = size
            if "saved_relationships_included" in result["provenance"]:
                result["provenance"]["saved_relationships_included"] = len(result["relationships"])
            return result
        result["truncated"] = True
        if not result["notes"]:
            raise ValueError("Recall allowance cannot fit the evidence packet.")
        note = result["notes"][-1]
        length = max(0, len(note["text"]) - (size - max_chars))
        if not length:
            result["notes"].pop()
        else:
            note["text"] = note["text"][:length]
            note["excerpt_end"] = note["excerpt_start"] + length


class NoteRecallService:
    """Keep one local encoder warm; all cached evidence stays content-bound.

    ``encoder_factory(options, cache_directory)`` is injectable for offline
    tests. Source embeddings are reused, never created as a search side effect.
    """
    def __init__(self, *, encoder_factory=None, override_root=None, annotation_resolver=None):
        self._lock = threading.RLock()
        self._factory = encoder_factory
        self.annotation_resolver = annotation_resolver
        self._documents = DocumentService(override_root=override_root or _DEFAULT_OVERRIDES)
        self._workspace = None
        self._workspace_id = None
        self._encoder = None
        self._encoder_key = None
        self._vector_cache = None
        self._query_cache = {}
        self._corpus_cache = None
        self._search_cache = None
        self._vector_generation = 0

    def close(self):
        with self._lock:
            self._documents.close()
            self._workspace = self._workspace_id = self._encoder = self._encoder_key = self._vector_cache = None
            self._query_cache.clear()
            self._corpus_cache = self._search_cache = None

    def _model(self, root, model_path, chunk_tokens):
        from .local_mapping import LocalMappingOptions, _LocalMiniLM
        model = Path(model_path).expanduser()
        if self._factory is None and not model.is_dir():
            raise ValueError("configured_local_model_unavailable")
        signature = []
        if self._factory is None:
            for path in model.rglob("*"):
                if path.is_file():
                    signature.append((str(path.relative_to(model)), _stamp(path)))
                    if len(signature) > 256:
                        raise ValueError("local_model_asset_limit")
        key = (str(model.resolve()), chunk_tokens, tuple(sorted(signature)))
        if self._encoder_key != key:
            cache = _safe_path(root, ".auto-zettelkasten/recall")
            cache.mkdir(parents=True, exist_ok=True)
            options = LocalMappingOptions(model_path=str(model), chunk_tokens=chunk_tokens, batch_size=8, threads=2)
            self._encoder = (self._factory or _LocalMiniLM)(options, cache)
            self._encoder_key = key
            self._vector_cache = None
            self._query_cache.clear()
        return self._encoder

    @staticmethod
    def _embedding(encoder, chunk_tokens=256):
        from .local_mapping import CHUNKING_VERSION
        return {"encoder": encoder.identity, "cleaning_version": CLEANING_VERSION,
                "chunking_version": CHUNKING_VERSION, "chunk_tokens": chunk_tokens,
                "aggregation": "mean_l2_chunk_vectors_then_l2"}

    @staticmethod
    def _note_vector_key(row, raw, embedding_identity):
        return _digest({"key_version": 2, "embedding_identity": embedding_identity,
                        "source_id": row["source_id"], "note_path": row["note_path"],
                        "semantic_sha256": raw["semantic_sha256"]})

    @staticmethod
    def _legacy_vector_key(row, raw, embedding_identity):
        return _digest({"embedding_identity": embedding_identity, "source_id": row["source_id"],
                        "note_path": row["note_path"], "note_content_sha256": raw["sha256"],
                        "content_sha256": raw["source_content_sha256"]})

    def _dedicated_semantic(self, root, corpus, raw_sources, model_path):
        reader = _CacheReader(root)
        record = reader.object(".auto-zettelkasten/recall/identity.json", 32 * 1024)
        identity = record.get("identity")
        if (record.get("schema_version") != _RECALL_SCHEMA or not isinstance(identity, dict)
                or record.get("embedding_identity") != _digest(identity)):
            raise ValueError("invalid_note_recall_identity")
        encoder = self._model(root, model_path, 256)
        if identity != self._embedding(encoder):
            raise ValueError("incompatible_note_recall_identity")
        cache_key = ("notes", str(root), _stamp(reader.root / ".auto-zettelkasten/recall/identity.json"),
               _digest({sid: source["sha256"] for sid, source in raw_sources.items()}), self._encoder_key)
        previous = self._vector_cache
        if previous and previous["key"] == cache_key and all(_stamp(Path(p)) == value for p, value in previous["stamps"].items()):
            return previous["vectors"], encoder, previous["status"]
        vector_directory = _safe_path(root, Path(".auto-zettelkasten/recall/vectors") / record["embedding_identity"])
        if vector_directory.is_dir():
            reader.stamps[str(vector_directory)] = _stamp(vector_directory)
        legacy = None
        vectors, invalid = {}, 0
        for row in corpus:
            raw = raw_sources[row["source_id"]]
            key = self._note_vector_key(row, raw, record["embedding_identity"])
            relative = Path(".auto-zettelkasten/recall/vectors") / record["embedding_identity"] / (key + ".json")
            if not _safe_path(root, relative).is_file():
                # Old receipts remain immutable. A metadata-only edit changes
                # their byte key, but identical cleaned text still binds the
                # vector. Validate the historical key and receipt below.
                if legacy is None:
                    legacy = {}
                    for scanned, old in enumerate(sorted(vector_directory.glob("*.json"))):
                        if scanned >= 20_000:
                            raise ValueError("note_recall_legacy_inventory_limit")
                        value = reader.object(old.relative_to(root), 64 * 1024)
                        if value.get("key_version") is None:
                            legacy[(value.get("source_id"), value.get("note_path"), value.get("content_sha256"))] = (old, value)
                found = legacy.get((row["source_id"], row["note_path"], raw["source_content_sha256"]))
                if found is None:
                    continue
                old, vector_record = found
                key, relative = old.stem, old.relative_to(root)
            else:
                vector_record = reader.object(relative, 64 * 1024)
            try:
                payload = {k: v for k, v in vector_record.items() if k != "record_sha256"}
                if (vector_record.get("record_sha256") != _digest(payload)
                        or payload.get("key") != key or payload.get("embedding_identity") != record["embedding_identity"]
                        or payload.get("source_id") != row["source_id"]
                        or payload.get("note_path") != row["note_path"] or len(payload.get("vector", [])) != encoder.dimension):
                    raise ValueError("stale_note_vector")
                if payload.get("key_version") == 2:
                    if payload.get("semantic_sha256") != raw["semantic_sha256"] or key != self._note_vector_key(row, raw, record["embedding_identity"]):
                        raise ValueError("stale_note_vector")
                elif (payload.get("key_version") is not None
                      or payload.get("content_sha256") != raw["source_content_sha256"]
                      or key != self._legacy_vector_key(row, {**raw, "sha256": payload.get("note_content_sha256")}, record["embedding_identity"])):
                    raise ValueError("stale_note_vector")
                values = payload["vector"]
                if any(isinstance(v, bool) or not isinstance(v, Real) or not math.isfinite(v) for v in values):
                    raise ValueError("invalid_note_vector")
                if not math.isclose(sum(v * v for v in values), 1., rel_tol=2e-4, abs_tol=2e-4):
                    raise ValueError("invalid_note_vector")
                vectors[row["source_id"]] = values
            except (ValueError, TypeError):
                invalid += 1
        if not reader.verify():
            raise ValueError("note_recall_cache_changed_during_read")
        status = {"status": "complete" if len(vectors) == len(corpus) else "partial" if vectors else "unavailable",
                                   "reason": "verified_full_note_index", "source_count": len(vectors),
                                   "invalid_vector_count": invalid, "embedding_identity": record["embedding_identity"],
                                   "automatic_source_embedding": False}
        self._vector_generation += 1
        self._vector_cache = {"key": cache_key, "vectors": vectors, "status": status, "stamps": dict(reader.stamps)}
        return vectors, encoder, status

    def _semantic(self, root, corpus, raw_sources, model_path):
        if _safe_path(root, ".auto-zettelkasten/recall/identity.json").is_file():
            if not model_path:
                return {}, None, {"status": "unavailable", "reason": "local_model_not_configured", "source_count": 0}
            vectors, encoder, status = self._dedicated_semantic(root, corpus, raw_sources, model_path)
            if len(vectors) < len(corpus) and _safe_path(root, ".auto-zettelkasten/mapping/result.json").is_file():
                try:
                    previous, _, mapped_status = self._mapped_semantic(root, raw_sources, model_path)
                    if mapped_status.get("embedding_identity") == status.get("embedding_identity"):
                        reused = len(set(previous) - set(vectors))
                        vectors = {**previous, **vectors}
                        status = {**status, "source_count": len(vectors),
                                  "status": "complete" if len(vectors) == len(corpus) else "partial",
                                  "mapped_vectors_reused": reused}
                except (OSError, ValueError, RuntimeError, ImportError, KeyError, TypeError):
                    # A damaged historical cache cannot invalidate current
                    # independently verified note-vector checkpoints.
                    pass
            return vectors, encoder, status
        return self._mapped_semantic(root, raw_sources, model_path)

    def _mapped_semantic(self, root, raw_sources, model_path):
        from .local_mapping import CHUNKING_VERSION, SCHEMA_VERSION
        result_path = _safe_path(root, ".auto-zettelkasten/mapping/result.json")
        if not result_path.is_file():
            return {}, None, {"status": "unavailable", "reason": "no_saved_note_embeddings"}
        if not model_path:
            return {}, None, {"status": "unavailable", "reason": "local_model_not_configured"}
        reader = _CacheReader(root)
        result = reader.object(result_path.relative_to(root), 64 * 1024 * 1024)
        identity = result.get("identity")
        if not isinstance(identity, dict) or result.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported_saved_embedding_contract")
        if identity.get("digest") != _digest({k: v for k, v in identity.items() if k != "digest"}):
            raise ValueError("saved_embedding_identity_checksum_mismatch")
        if (identity.get("cleaning_version") != CLEANING_VERSION or identity.get("chunking_version") != CHUNKING_VERSION
                or identity.get("aggregation") != "mean_l2_chunk_vectors_then_l2"
                or type(identity.get("chunk_tokens")) is not int or not 1 <= identity["chunk_tokens"] <= 256):
            raise ValueError("incompatible_saved_embedding_processing")
        encoder = self._model(root, model_path, identity["chunk_tokens"])
        if identity.get("encoder") != encoder.identity:
            raise ValueError("incompatible_saved_embedding_encoder")
        dimension = encoder.dimension
        if type(dimension) is not int or not 1 <= dimension <= 4096 or encoder.identity.get("dimension") != dimension:
            raise ValueError("invalid_saved_embedding_dimension")
        source_hashes = _digest({sid: value["sha256"] for sid, value in raw_sources.items()})
        key = (str(root), _stamp(result_path), source_hashes, self._encoder_key)
        previous = self._vector_cache
        if previous and previous["key"] == key and all(_stamp(Path(p)) == value for p, value in previous["stamps"].items()):
            return previous["vectors"], encoder, previous["status"]
        embedding = {k: identity[k] for k in ("encoder", "cleaning_version", "chunking_version", "chunk_tokens", "aggregation")}
        digest = _digest(embedding)
        chunks, sources = result.get("chunks"), result.get("sources")
        if not isinstance(chunks, list) or len(chunks) > 100_000 or not isinstance(sources, list) or len(sources) > 20_000:
            raise ValueError("saved_embedding_inventory_limit")
        by_chunk = {}
        for chunk in chunks:
            cid = chunk.get("chunk_id") if isinstance(chunk, dict) else None
            if not isinstance(cid, str) or not re_hex(cid) or cid in by_chunk:
                raise ValueError("invalid_saved_chunk_identity")
            by_chunk[cid] = chunk
        vectors, excluded, seen = {}, Counter(), set()
        for source in sources:
            sid = source.get("source_id") if isinstance(source, dict) else None
            if not isinstance(sid, str) or sid in seen:
                raise ValueError("invalid_saved_source_identity")
            seen.add(sid)
            current = raw_sources.get(sid)
            if current is None:
                continue
            current = current.get("mapping_baseline", current)
            if (source.get("full_text_sha256") != current["sha256"] or source.get("note_path") != current["path"]
                    or source.get("embedding_text_sha256") != _digest([span["text"] for span in current["clean"]["spans"]])):
                excluded["stale_or_different_note_text"] += 1
                continue
            chunk_ids = source.get("chunk_ids")
            if not isinstance(chunk_ids, list) or not chunk_ids or len(chunk_ids) > 10_000 or len(set(chunk_ids)) != len(chunk_ids):
                excluded["no_complete_note_vector"] += 1
                continue
            values = []
            for cid in chunk_ids:
                chunk = by_chunk.get(cid)
                if not chunk or chunk.get("source_id") != sid or chunk.get("identity") != source.get("identity"):
                    raise ValueError("saved_chunk_source_binding_mismatch")
                pieces, last = [], 0
                spans = chunk.get("source_spans", [])
                if not isinstance(spans, list) or not spans or len(spans) > 20_000:
                    raise ValueError("invalid_saved_chunk_offsets")
                for span in spans:
                    left, right = span.get("start"), span.get("end")
                    if type(left) is not int or type(right) is not int or not last <= left < right <= len(current["text"]):
                        raise ValueError("invalid_saved_chunk_offsets")
                    pieces.append(current["text"][left:right]); last = right
                if _sha("".join(pieces)) != chunk.get("text_sha256"):
                    raise ValueError("saved_chunk_text_checksum_mismatch")
                content = {k: chunk[k] for k in ("source_id", "identity", "owner", "section_kind", "text_sha256", "occurrence")}
                if _digest({"embedding_identity": digest, **content}) != cid:
                    raise ValueError("saved_chunk_identity_checksum_mismatch")
                base = Path(".auto-zettelkasten/mapping/chunks") / digest / cid
                receipt = reader.object(base.with_suffix(".json"), 8192)
                binary = reader.read(base.with_suffix(".f32"), dimension * 4)
                if (len(binary) != dimension * 4 or receipt.get("chunk_id") != cid
                        or receipt.get("embedding_identity") != digest
                        or receipt.get("vector_sha256") != hashlib.sha256(binary).hexdigest()):
                    raise ValueError("saved_vector_checksum_mismatch")
                vector = list(struct.unpack("<" + "f" * dimension, binary))
                if any(not math.isfinite(v) for v in vector) or not math.isclose(sum(v * v for v in vector), 1., rel_tol=2e-4, abs_tol=2e-4):
                    raise ValueError("saved_vector_not_normalized")
                values.append(vector)
            vectors[sid] = _unit([sum(row[i] for row in values) / len(values) for i in range(dimension)])
        if not reader.verify():
            raise ValueError("saved_embeddings_changed_during_read")
        status = {"status": "complete" if len(vectors) == len(raw_sources) else "partial" if vectors else "unavailable",
                  "reason": "verified_saved_note_embeddings" if vectors else "no_current_compatible_note_vectors",
                  "source_count": len(vectors), "excluded_sources": dict(excluded), "embedding_identity": digest,
                  "automatic_source_embedding": False}
        self._vector_cache = {"key": key, "vectors": vectors, "status": status, "stamps": dict(reader.stamps)}
        self._vector_generation += 1
        return vectors, encoder, status

    def _corpus_for(self, root, snapshot, instance_id=None, library_id=None):
        annotations = {}
        if self.annotation_resolver is not None:
            for row in snapshot["documents"]:
                try:
                    receipt = self.annotation_resolver(root, row["path"])
                except (OSError, ValueError):
                    receipt = None
                if isinstance(receipt, Mapping) and receipt:
                    annotations[row["path"]] = receipt
        key = (str(root), tuple(_snapshot_identity(snapshot)), instance_id, library_id, _digest(annotations))
        if not self._corpus_cache or self._corpus_cache[0] != key:
            self._corpus_cache = (key, _corpus(snapshot, instance_id=instance_id, library_id=library_id,
                                            annotation_snapshots=annotations))
            self._search_cache = None
        return self._corpus_cache[1]

    def _open_documents(self, workspace, documents=None, workspace_id=None):
        if documents is None:
            root = Path(workspace).expanduser()
            if self._workspace != root:
                opened = self._documents.open(root)
                self._workspace, self._workspace_id = root, opened["id"]
            documents, workspace_id = self._documents, self._workspace_id
        root = Path(documents.workspace_path(workspace_id))
        if workspace is not None and Path(workspace).expanduser().resolve() != root.resolve():
            raise DocumentError("Recall workspace does not match the opened Markdown workspace.")
        return root, documents, workspace_id

    @staticmethod
    def _configured_model(root, model_path):
        if model_path:
            return model_path
        try:
            from .local_tools import _ReadBudget
            budget = _ReadBudget(root, max_files=1, max_file_bytes=1024 * 1024, max_total_bytes=1024 * 1024)
            config = root / "auto-zettelkasten.yml"; budget.reserve([config])
            local = budget.read(config).get("local_mapping", {})
            if isinstance(local, Mapping) and isinstance(local.get("model_path"), str):
                return local["model_path"]
        except (OSError, ValueError):
            pass
        return ""

    def prepare(self, workspace, *, model_path="", documents=None, workspace_id=None,
                max_wall_seconds=600., min_free_disk_bytes=1024 ** 3, max_cache_bytes=1024 ** 3,
                progress=None, max_changed_notes=None, source_ids=None):
        """Explicitly build local note vectors, checkpointing each complete note.

        No original documents or graph are written. Search can use completed
        checkpoints even when a resource bound stops this preparation early.
        """
        from .files import atomic_write_text
        from .local_mapping import _exclusive_cache, _semantic_chunks
        if (isinstance(max_wall_seconds, bool) or not isinstance(max_wall_seconds, Real)
                or not math.isfinite(max_wall_seconds) or not 0 < max_wall_seconds <= 3600
                or type(min_free_disk_bytes) is not int or min_free_disk_bytes < 1024 ** 2
                or type(max_cache_bytes) is not int or not 1024 <= max_cache_bytes <= 2 * 1024 ** 3):
            raise ValueError("Invalid local recall indexing resource limits.")
        if not isinstance(model_path, str) or "\x00" in model_path or len(model_path) > 4096:
            raise ValueError("The local model path is invalid.")
        if max_changed_notes is not None and (type(max_changed_notes) is not int or not 1 <= max_changed_notes <= 64):
            raise ValueError("Incremental indexing admits 1–64 changed notes per refresh.")
        if source_ids is not None and (not isinstance(source_ids, (list, tuple, set, frozenset))
                or len(source_ids) > 64 or any(not isinstance(sid, str) or not 0 < len(sid) <= 256 for sid in source_ids)):
            raise ValueError("Incremental indexing requires at most 64 source identities.")
        with self._lock:
            root, documents, workspace_id = self._open_documents(workspace, documents, workspace_id)
            snapshot = documents.recall_corpus(workspace_id)
            corpus, raw_sources, excluded = self._corpus_for(root, snapshot)
            model_path = self._configured_model(root, model_path)
            if not model_path:
                raise ValueError("Configure an existing local MiniLM model before building the search index.")
            cache = _safe_path(root, ".auto-zettelkasten/recall")
            cache.mkdir(parents=True, exist_ok=True)
            started = time.monotonic()
            def check():
                if time.monotonic() - started > max_wall_seconds:
                    raise RuntimeError("local_recall_wall_limit")
                if shutil.disk_usage(cache).free < min_free_disk_bytes:
                    raise RuntimeError("local_recall_free_disk_limit")
            check()
            with _exclusive_cache(cache):
                encoder = self._model(root, model_path, 256)
                embedding = self._embedding(encoder)
                identity = _digest(embedding)
                directory = _safe_path(root, Path(".auto-zettelkasten/recall/vectors") / identity)
                directory.mkdir(parents=True, exist_ok=True)
                cache_bytes = 0
                for path in cache.rglob("*"):
                    if path.is_symlink():
                        raise ValueError("Recall cache cannot contain symbolic links.")
                    if path.is_file():
                        cache_bytes += path.stat().st_size
                    if cache_bytes > max_cache_bytes:
                        raise RuntimeError("local_recall_cache_limit")
                atomic_write_text(cache / "identity.json", _json({"schema_version": _RECALL_SCHEMA,
                                  "identity": embedding, "embedding_identity": identity}) + "\n")
                reused, completed, chunks_encoded, stop_reason = 0, 0, 0, None
                try:
                    cached, _, _ = self._dedicated_semantic(root, corpus, raw_sources, model_path)
                    for row in corpus:
                        check()
                        sid = row["source_id"]
                        if sid in cached:
                            reused += 1; completed += 1
                            continue
                        if source_ids is not None and sid not in source_ids:
                            continue
                        if max_changed_notes is not None and completed - reused >= max_changed_notes:
                            raise RuntimeError("local_recall_changed_note_limit")
                        source = raw_sources[sid]
                        chunks = _semantic_chunks(source["text"], source["clean"]["spans"], encoder.tokenizer, 256)
                        if not chunks or len(chunks) > 10_000 or chunks_encoded + len(chunks) > 100_000:
                            raise RuntimeError("local_recall_chunk_limit")
                        values = []
                        for offset in range(0, len(chunks), 8):
                            check()
                            batch = chunks[offset:offset + 8]
                            encoded = list(encoder.encode([chunk["text"] for chunk in batch]))
                            if len(encoded) != len(batch) or any(len(vector) != encoder.dimension for vector in encoded):
                                raise ValueError("Local note encoder returned an incompatible vector batch.")
                            values.extend(_unit(vector) for vector in encoded)
                            chunks_encoded += len(batch)
                            check()
                        vector = _unit([sum(v[i] for v in values) / len(values) for i in range(encoder.dimension)])
                        key = self._note_vector_key(row, source, identity)
                        record = {"key": key, "key_version": 2, "embedding_identity": identity, "source_id": sid,
                                  "note_path": row["note_path"], "content_sha256": source["source_content_sha256"],
                                  "semantic_sha256": source["semantic_sha256"],
                                  "note_content_sha256": source["sha256"], "vector": vector}
                        record["record_sha256"] = _digest(record)
                        serialized = _json(record) + "\n"
                        cache_bytes += len(serialized.encode("utf-8"))
                        if cache_bytes > max_cache_bytes:
                            raise RuntimeError("local_recall_cache_limit")
                        atomic_write_text(directory / (key + ".json"), serialized)
                        completed += 1
                        if progress:
                            progress({"phase": "indexing_notes", "completed": completed, "total": len(corpus)})
                except RuntimeError as exc:
                    if str(exc) not in {"local_recall_wall_limit", "local_recall_free_disk_limit", "local_recall_cache_limit", "local_recall_chunk_limit", "local_recall_changed_note_limit"}:
                        raise
                    stop_reason = str(exc)
                if _snapshot_identity(documents.recall_corpus(workspace_id)) != _snapshot_identity(snapshot):
                    stop_reason = "notes_changed_during_indexing"
                self._vector_cache = None
                return {"status": "partial" if stop_reason else "prepared", "searchable_note_count": len(corpus),
                        "indexed_note_count": completed, "reused_note_count": reused,
                        "encoded_chunks": chunks_encoded, "embedding_identity": identity,
                        "excluded_notes": excluded, "document_index_partial": snapshot["truncated"],
                        "stop_reason": stop_reason, "seconds": time.monotonic() - started,
                        "provider_calls": 0, "jev": {"enabled": False, "provider_calls": 0}}

    def recall(self, workspace, query, *, max_notes=8, max_chars=12_000, model_path="", documents=None,
               workspace_id=None, instance_id=None, library_id=None):
        if not isinstance(query, str) or not query.strip() or len(query) > 2000 or "\x00" in query:
            raise ValueError("Recall query must contain 1–2,000 characters.")
        if type(max_notes) is not int or not 1 <= max_notes <= 32 or type(max_chars) is not int or not 64 <= max_chars <= 64_000:
            raise ValueError("Recall limits must be 1–32 notes and 64–64,000 characters.")
        for value in (instance_id, library_id):
            if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 200 or "\x00" in value):
                raise ValueError("Recall source scope must be a bounded text identity.")
        if not isinstance(model_path, str) or "\x00" in model_path or len(model_path) > 4096:
            raise ValueError("The local model path is invalid.")
        with self._lock:
            root, documents, workspace_id = self._open_documents(workspace, documents, workspace_id)
            snapshot = documents.recall_corpus(workspace_id)
            corpus, raw_sources, excluded = self._corpus_for(root, snapshot, instance_id, library_id)
            model_path = self._configured_model(root, model_path)
            semantic = {"status": "unavailable", "reason": "empty_note_corpus", "source_count": 0}
            vectors, query_vector = {}, None
            if corpus:
                try:
                    vectors, encoder, semantic = self._semantic(root, corpus, raw_sources, model_path)
                    if vectors:
                        key = (_digest(encoder.identity), query)
                        if key not in self._query_cache:
                            from .local_mapping import _token_chunks
                            pieces = [row[2] for row in _token_chunks(query, 0, encoder.tokenizer, encoder.options.chunk_tokens)]
                            if not pieces:
                                raise ValueError("Local query contains no usable semantic tokens.")
                            encoded = list(encoder.encode(pieces))
                            if len(encoded) != len(pieces):
                                raise ValueError("Local query encoder returned the wrong number of vectors.")
                            normalized = [_unit(row) for row in encoded]
                            self._query_cache[key] = _unit([sum(row[i] for row in normalized) / len(normalized)
                                                           for i in range(encoder.dimension)])
                            if len(self._query_cache) > 64:
                                del self._query_cache[next(iter(self._query_cache))]
                        query_vector = self._query_cache[key]
                except (OSError, ValueError, RuntimeError, ImportError, KeyError, TypeError, RecursionError) as exc:
                    vectors, query_vector = {}, None
                    semantic = {"status": "unavailable", "reason": "local_semantic_search_unavailable",
                                "failure_type": type(exc).__name__, "source_count": 0, "automatic_source_embedding": False}
            key = (self._corpus_cache[0], self._vector_generation if vectors else None)
            if not self._search_cache or self._search_cache[0] != key:
                self._search_cache = (key, HybridSearchIndex(corpus, vectors=vectors))
            index = self._search_cache[1]
            result = index.recall(query, query_vector=query_vector, max_notes=max_notes, max_chars=max_chars)
            relationships = documents.recall_relationships(workspace_id, [row["note_path"] for row in result["notes"]])
            selected, budget = [], min(3000, max_chars // 4)
            for relation in relationships:
                if "selected_evidence" in relation:
                    original = {source["path"]: source["text"] for source in raw_sources.values()}
                    relation = {**relation, "selected_evidence": [passage for passage in relation["selected_evidence"]
                                if passage.get("path") in original and isinstance(passage.get("text"), str)
                                and passage["text"] in original[passage["path"]]]}
                if len(_json([*selected, relation])) <= budget:
                    selected.append(relation)
            if selected:
                reserved = len(_json(selected)) - 2
                result = index.recall(query, query_vector=query_vector, max_notes=max_notes, max_chars=max_chars - reserved)
                paths = {row["note_path"] for row in result["notes"]}
                result["relationships"] = [row for row in selected if row["source"] in paths and row["target"] in paths]
                result["chars"] = len(_json({k: result[k] for k in ("notes", "relationships")}))
                result["provenance"]["saved_relationships_included"] = len(result["relationships"])
            result = _annotation_pointers(result, raw_sources, max_chars)
            if _snapshot_identity(documents.recall_corpus(workspace_id)) != _snapshot_identity(snapshot):
                raise DocumentIndexChanged()
            coverage = {"note_count": len(snapshot["documents"]), "searchable_note_count": len(corpus),
                        "excluded_notes": excluded, "document_index_partial": snapshot["truncated"],
                        "skipped_note_count": snapshot["skipped_note_count"], "semantic": semantic,
                        "scope": "workspace_notes_known_source_scopes_filtered" if instance_id or library_id else "workspace_notes"}
            return {**result, "status": "retrieved", "coverage": coverage, "provider_calls": 0,
                    "warnings": list(snapshot["warnings"]), "jev": {"enabled": False, "provider_calls": 0}}


def re_hex(value):
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


_SERVICE = None
_SERVICE_LOCK = threading.Lock()


def _default_service():
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            from .zotero_workspace_service import read_annotation_snapshot
            private_root = Path.home() / ".config/auto-zettelkasten/zotero-workspace"
            _SERVICE = NoteRecallService(annotation_resolver=lambda workspace, path:
                read_annotation_snapshot(private_root, workspace, path))
        return _SERVICE


def recall_workspace(workspace, query, **options):
    """Agent/CLI entry point sharing the same warm local retrieval service."""
    return _default_service().recall(workspace, query, **options)


def prepare_workspace_recall(workspace, **options):
    """Explicit local indexing entry point; never called by ordinary recall."""
    return _default_service().prepare(workspace, **options)
