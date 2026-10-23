"""Local derived semantic maps; similarities and communities are suggestions only.

Inputs already belong to the workspace catalogue. This module never fetches text,
downloads a model, changes a note, writes Zotero, or accepts a relationship. Full
input text is kept separately from the representation used by the encoder.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields, replace
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import time
from typing import Any

from .files import atomic_write_bytes, atomic_write_text, sha256_file, sha256_text, write_json
from .retrieval import LocalRetrievalIndex


SCHEMA_VERSION = "local-semantic-map-v1"
MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
CACHE_RELATIVE_PATH = Path(".auto-zettelkasten") / "mapping"
_IDENTITY_FIELDS = ("instance_id", "library_type", "library_id", "item_key")


class LocalMappingError(RuntimeError):
    """An actionable local dependency, model, cache or resource failure."""


@dataclass(frozen=True)
class LocalMappingOptions:
    tuning_mode: str = "manual"
    encoder: str = "minilm"
    clustering: str = "leiden"
    model_path: str | None = None
    batch_size: int = 8
    chunk_tokens: int = 256
    neighbors: int = 8
    seed: int = 0
    resolution: float = 1.0
    min_similarity: float = 0.0
    min_cluster_size: int = 3
    threads: int = 2
    max_sources: int = 10_000
    max_chunks: int = 100_000
    max_text_bytes: int = 64 * 1024 * 1024
    max_wall_seconds: float = 600.0
    min_free_disk_bytes: int = 1024 * 1024 * 1024
    max_cache_bytes: int = 1024 * 1024 * 1024

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None = None) -> "LocalMappingOptions":
        if value is not None and not isinstance(value, Mapping):
            raise ValueError("local mapping settings must be an object")
        value = {} if value is None else dict(value)
        unexpected = set(value) - {field.name for field in fields(cls)}
        if unexpected:
            raise ValueError(f"unknown local mapping settings: {', '.join(sorted(unexpected))}")
        result = cls(**value)
        result.validate()
        return result

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    from_dict = from_mapping

    def validate(self) -> None:
        if not isinstance(self.tuning_mode, str) or self.tuning_mode not in {"auto", "manual"}:
            raise ValueError("tuning_mode must be auto or manual")
        if self.encoder != "minilm":
            raise ValueError("This local mapper supports encoder='minilm'; experimental encoders require an explicit compatible adapter.")
        if self.clustering not in {"leiden", "hdbscan"}:
            raise ValueError("clustering must be 'leiden' or 'hdbscan'")
        limits = {"batch_size": 32, "chunk_tokens": 256, "neighbors": 128, "threads": 8,
                  "max_sources": 100_000, "max_chunks": 1_000_000}
        for name in ("batch_size", "chunk_tokens", "neighbors", "threads", "max_sources", "max_chunks",
                     "min_cluster_size", "max_text_bytes", "min_free_disk_bytes", "max_cache_bytes"):
            value = getattr(self, name)
            if type(value) is not int or value < 1 or value > limits.get(name, 2**40):
                raise ValueError(f"{name} must be a positive integer within its local resource limit")
        if self.chunk_tokens < 8 or self.min_cluster_size < 2:
            raise ValueError("chunk_tokens must be at least 8; min_cluster_size must be at least 2")
        if type(self.seed) is not int or not 0 <= self.seed < 2**31:
            raise ValueError("seed must be an integer from 0 to 2**31-1")
        for name in ("resolution", "max_wall_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if isinstance(self.min_similarity, bool) or not isinstance(self.min_similarity, (float, int)) or not math.isfinite(self.min_similarity) or not 0 <= self.min_similarity <= 1:
            raise ValueError("min_similarity must be between 0 and 1")
        if self.model_path is not None and (not isinstance(self.model_path, str) or not self.model_path.strip()):
            raise ValueError("model_path must identify an existing local model directory")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return sha256_text(_json(value))


def _numpy():
    try:
        import numpy
    except ImportError as exc:
        raise LocalMappingError("Local mapping needs the 'local-mapping' optional dependencies; install auto-zettelkasten[local-mapping].") from exc
    return numpy


def _versions(algorithm: str) -> dict[str, str]:
    result = {}
    for name in (("hdbscan",) if algorithm == "hdbscan" else ("igraph", "leidenalg")):
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = "unknown"
    return result


def _cluster_dependencies(algorithm: str) -> tuple[Any, Any | None]:
    try:
        if algorithm == "hdbscan":
            import hdbscan
            return hdbscan, None
        import igraph
        import leidenalg
        return igraph, leidenalg
    except ImportError as exc:
        extra = "local-mapping-hdbscan" if algorithm == "hdbscan" else "local-mapping"
        raise LocalMappingError(f"{algorithm} is unavailable; install auto-zettelkasten[{extra}]. No provider or alternative clustering was run.") from exc


def _asset_identity(model_path: Path, cache_dir: Path) -> dict[str, Any]:
    """Hash changed assets, retaining hashes only while size/mtime/ctime agree."""
    if not model_path.is_dir():
        raise LocalMappingError("local_mapping.model_path must point to an existing cached all-MiniLM-L6-v2 model directory. This mapper does not download models.")
    required = ("config.json", "modules.json", "tokenizer_config.json")
    if any(not (model_path / name).is_file() for name in required):
        raise LocalMappingError("The local MiniLM directory is incomplete (config, tokenizer or sentence-transformer modules missing).")
    files = sorted(path for path in model_path.rglob("*") if path.is_file() and path.suffix in {".json", ".txt", ".safetensors", ".bin", ".model"})
    if not any(path.suffix in {".safetensors", ".bin"} for path in files):
        raise LocalMappingError("The cached MiniLM model has no local weight file; no download was attempted.")
    if len(files) > 256:
        raise LocalMappingError("The local model directory exceeds the bounded asset inventory.")
    manifest_path = cache_dir / "model-assets.json"
    try:
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    old = previous.get("files", {}) if previous.get("model_path") == str(model_path.resolve()) else {}
    entries = {}
    for path in files:
        relative = path.relative_to(model_path).as_posix()
        stat = path.stat()
        stamp = [stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
        prior = old.get(relative, {})
        entries[relative] = {"stamp": stamp, "sha256": prior["sha256"] if prior.get("stamp") == stamp and re.fullmatch(r"[a-f0-9]{64}", str(prior.get("sha256", ""))) else sha256_file(path)}
    write_json(manifest_path, {"model_path": str(model_path.resolve()), "files": entries})
    config = json.loads((model_path / "config.json").read_text(encoding="utf-8"))
    if config.get("hidden_size") != 384 or config.get("num_hidden_layers") != 6:
        raise LocalMappingError("The configured model is not the supported MiniLM-L6 384-dimensional architecture.")
    modules = json.loads((model_path / "modules.json").read_text(encoding="utf-8"))
    pooling_modules = [entry for entry in modules if str(entry.get("type", "")).endswith(".Pooling")]
    if len(pooling_modules) != 1:
        raise LocalMappingError("The cached MiniLM model must declare one mean-pooling module.")
    pool_path = Path(str(pooling_modules[0].get("path", "")))
    if pool_path.is_absolute() or ".." in pool_path.parts:
        raise LocalMappingError("Invalid local pooling-module path.")
    pool = json.loads((model_path / pool_path / "config.json").read_text(encoding="utf-8"))
    if pool.get("pooling_mode_mean_tokens") is not True or any(value for key, value in pool.items() if key.startswith("pooling_mode_") and key != "pooling_mode_mean_tokens"):
        raise LocalMappingError("The local model pooling differs from the supported MiniLM mean-pooling contract.")
    return {"model_sha256": _digest({name: item["sha256"] for name, item in entries.items()}),
            "model_revision": str(config.get("_commit_hash") or "local-content-addressed"),
            "asset_bytes": sum(item["stamp"][0] for item in entries.values())}


class _LocalMiniLM:
    """Tokenizer is local; the encoder is loaded lazily, once, only for new rows."""
    dimension = 384
    max_seq_length = 256

    def __init__(self, options: LocalMappingOptions, cache_dir: Path):
        if not options.model_path:
            raise LocalMappingError("Set local_mapping.model_path to an already cached all-MiniLM-L6-v2 directory. Local mapping never downloads a model or uses a provider key.")
        model_path = Path(options.model_path).expanduser().resolve()
        try:
            assets = _asset_identity(model_path, cache_dir)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise LocalMappingError("The local MiniLM assets could not be verified; check the cached configuration and weights.") from exc
        try:
            from transformers import AutoTokenizer
            self.tokenizer = AutoTokenizer.from_pretrained(str(model_path), local_files_only=True, trust_remote_code=False)
            versions = {name: importlib.metadata.version(name) for name in ("numpy", "torch", "transformers", "sentence-transformers")}
        except (ImportError, importlib.metadata.PackageNotFoundError) as exc:
            raise LocalMappingError("Local MiniLM needs auto-zettelkasten[local-mapping]; no model was loaded or downloaded.") from exc
        except Exception as exc:
            raise LocalMappingError(f"The cached MiniLM tokenizer could not be opened locally: {type(exc).__name__}") from exc
        self.identity = {"model_name": MODEL_NAME, **assets, "dimension": self.dimension,
                         "pooling": "mean_attention_mask", "normalize": True,
                         "dtype": "float32", "device": "cpu", "library_versions": versions}
        self.options = options
        self.model_path = model_path
        self.model = None
        self.model_load_seconds = 0.0

    def encode(self, texts: Sequence[str]):
        if self.model is None:
            started = time.monotonic()
            try:
                import torch
                from sentence_transformers import SentenceTransformer
                torch.set_num_threads(self.options.threads)
                self.model = SentenceTransformer(str(self.model_path), device="cpu", local_files_only=True, trust_remote_code=False)
                self.model.float()
                if self.model.get_sentence_embedding_dimension() != self.dimension or self.model.max_seq_length < self.options.chunk_tokens:
                    raise LocalMappingError("The local MiniLM embedding dimension or token window differs from the supported contract.")
            except LocalMappingError:
                raise
            except Exception as exc:
                raise LocalMappingError(f"The cached MiniLM encoder could not be opened locally: {type(exc).__name__}") from exc
            self.model_load_seconds = time.monotonic() - started
        return self.model.encode(list(texts), batch_size=self.options.batch_size, show_progress_bar=False,
                                 convert_to_numpy=True, normalize_embeddings=True)


@contextmanager
def _exclusive_cache(path: Path):
    """OS locks release on crash; a persistent inode prevents stale-lock races."""
    lock = path / "job.lock"
    try:
        existing = lock.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None and (not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1):
        raise LocalMappingError("The derived mapping lock must be a dedicated regular cache file, without symbolic or hard links.")
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), 0o600)
    except OSError as exc:
        raise LocalMappingError("The derived mapping lock could not be opened safely.") from exc
    # Keep the inode: deleting a released file races with the next opener.
    with os.fdopen(descriptor, "r+b") as handle:
        def validate_lock() -> None:
            try:
                opened, current = os.fstat(handle.fileno()), lock.lstat()
            except OSError as exc:
                raise LocalMappingError("The derived mapping lock disappeared or could not be verified safely.") from exc
            if (not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode)
                    or opened.st_nlink != 1 or current.st_nlink != 1
                    or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
                raise LocalMappingError("The derived mapping lock changed or is linked outside its cache.")

        validate_lock()
        if os.name == "nt":
            import msvcrt
            if not lock.stat().st_size:
                handle.write(b" ")
                handle.flush()
            handle.seek(0)
            acquire = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            release = lambda: (handle.seek(0), msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1))
        else:
            import fcntl
            acquire = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        try:
            acquire()
        except OSError as exc:
            raise LocalMappingError("Another local mapping job owns this cache. Wait for it to finish before refreshing.") from exc
        try:
            validate_lock()
            handle.seek(0)
            handle.write(_json({"pid": os.getpid()}).encode("utf-8"))
            handle.truncate()
            handle.flush()
            yield
        finally:
            release()


def _cache_size(path: Path) -> int:
    total = 0
    for entry in path.rglob("*"):
        if entry.is_symlink():
            raise LocalMappingError("The derived mapping cache must not contain symbolic links.")
        if entry.is_file():
            total += entry.stat().st_size
    return total


def _token_chunks(text: str, start: int, tokenizer: Any, token_limit: int) -> list[tuple[int, int, str]]:
    """Partition every token without truncation; keep exact original offsets."""
    special = tokenizer.num_special_tokens_to_add(pair=False)
    budget = token_limit - special
    if budget < 1:
        raise LocalMappingError("The tokenizer leaves no room for source text.")
    result = []
    # Bound tokenizer preparation independently of document length.
    for block_start in range(0, len(text), 16_384):
        block = text[block_start:block_start + 16_384]
        try:
            tokenized = tokenizer(block, add_special_tokens=False, return_offsets_mapping=True, truncation=False, verbose=False)
            offsets = tokenized["offset_mapping"]
        except Exception as exc:
            raise LocalMappingError("The local tokenizer must provide exact offsets; no text was silently truncated.") from exc
        if len(offsets) != len(tokenized["input_ids"]):
            raise LocalMappingError("Tokenizer offsets do not match its token sequence.")
        for offset in range(0, len(offsets), budget):
            group = offsets[offset:offset + budget]
            left, right = int(group[0][0]), int(group[-1][1])
            if not 0 <= left < right <= len(block):
                raise LocalMappingError("Tokenizer returned invalid source offsets.")
            piece = block[left:right]
            # Re-tokenization at a substring boundary can add word pieces.
            count = len(tokenizer(piece, add_special_tokens=True, truncation=False, verbose=False)["input_ids"])
            if count > token_limit:
                # A smaller token group is exact, unlike silently clipping it.
                if len(group) <= 1:
                    raise LocalMappingError("A tokenizer fragment exceeds the configured token window.")
                midpoint = (left + right) // 2
                result.extend(_token_chunks(block[left:midpoint], start + block_start + left, tokenizer, token_limit))
                result.extend(_token_chunks(block[midpoint:right], start + block_start + midpoint, tokenizer, token_limit))
            else:
                result.append((start + block_start + left, start + block_start + right, piece))
    return result


def _unit_vectors(value: Any, count: int, dimension: int):
    np = _numpy()
    result = np.asarray(value, dtype=np.float32)
    if result.shape != (count, dimension) or not np.isfinite(result).all():
        raise LocalMappingError("The encoder returned invalid dimensions or nonfinite values; cached progress is preserved.")
    norms = np.linalg.norm(result, axis=1)
    if (norms <= 1e-12).any():
        raise LocalMappingError("The encoder returned an empty vector; cached progress is preserved.")
    return result / norms[:, None]


def _ranked_neighbors(ids, matrix, *, options, embedding_identity=""):
    """Exact search once, reusable across display and discovery policies."""
    neighbors = []
    started = time.monotonic()
    for first in range(0, len(ids), 64):
        if time.monotonic() - started > options.max_wall_seconds:
            raise LocalMappingError("Semantic neighbor refresh reached max_wall_seconds; no partial map was committed.")
        scores = matrix[first:first + 64] @ matrix.T
        for row, values in enumerate(scores):
            index = first + row
            ranked = sorted((other for other in range(len(ids)) if other != index and float(values[other]) > max(0.0, options.min_similarity)),
                            key=lambda other: (-float(values[other]), ids[other]))[:options.neighbors]
            for other in ranked:
                score = min(1.0, float(values[other]))
                neighbors.append({"source_id": ids[index], "target_source_id": ids[other], "score": score,
                                  "provenance": {"kind": "semantic_similarity", "evidence_backed": False,
                                                 "accepted": False, "embedding_identity": embedding_identity}})
    return neighbors


def cluster_source_vectors(source_ids: Sequence[str], vectors: Any, *, options: LocalMappingOptions | None = None,
                           embedding_identity: str = "") -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Exact bounded-memory cosine graph, followed by the selected local view."""
    options = options or LocalMappingOptions()
    options.validate()
    np = _numpy()
    if len(source_ids) > options.max_sources or len(set(source_ids)) != len(source_ids):
        raise ValueError("source IDs must be unique and within max_sources")
    if not source_ids:
        return [], []
    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.ndim != 2:
        raise ValueError("source vectors must be a matrix")
    matrix = _unit_vectors(matrix, len(source_ids), matrix.shape[1])
    order = sorted(range(len(source_ids)), key=lambda index: source_ids[index])
    ids = [source_ids[index] for index in order]
    matrix = matrix[order]
    neighbors = _ranked_neighbors(ids, matrix, options=options, embedding_identity=embedding_identity)
    return _communities_from_neighbors(ids, matrix, neighbors, options=options), neighbors


def _communities_from_neighbors(ids, matrix, neighbors, *, options):
    positions = {sid: index for index, sid in enumerate(ids)}
    edges: dict[tuple[int, int], float] = {}
    for row in neighbors:
        edge = tuple(sorted((positions[row["source_id"]], positions[row["target_source_id"]])))
        edges[edge] = max(edges.get(edge, 0), row["score"])
    if options.clustering == "hdbscan" and len(ids) < options.min_cluster_size:
        groups = []
        noise: set[str] = set(ids)
    elif options.clustering == "hdbscan":
        hdbscan, _ = _cluster_dependencies("hdbscan")
        labels = hdbscan.HDBSCAN(min_cluster_size=options.min_cluster_size, metric="euclidean",
                                core_dist_n_jobs=1, approx_min_span_tree=False).fit_predict(matrix)
        by_label: dict[int, list[str]] = defaultdict(list)
        noise = set()
        for source_id, label in zip(ids, labels, strict=True):
            if int(label) < 0:
                noise.add(source_id)
            else:
                by_label[int(label)].append(source_id)
        groups = list(by_label.values())
    elif len(ids) == 1 or not edges:
        groups = [[source_id] for source_id in ids]
        noise = set()
    else:
        igraph, leidenalg = _cluster_dependencies("leiden")
        keys = sorted(edges)
        graph = igraph.Graph(n=len(ids), edges=keys, directed=False)
        partition = leidenalg.find_partition(graph, leidenalg.RBConfigurationVertexPartition,
                                             weights=[edges[key] for key in keys], seed=options.seed,
                                             resolution_parameter=options.resolution, n_iterations=2)
        groups = [[ids[index] for index in group] for group in partition]
        noise = set()
    communities = [{"community_id": "semantic-" + _digest(sorted(group))[:20], "source_ids": sorted(group),
                    "algorithm": options.clustering} for group in groups]
    if noise:
        # Noise is a status, not one artificial community linking its members.
        communities.extend({"community_id": "semantic-noise-" + _digest(source_id)[:20], "source_ids": [source_id],
                            "algorithm": "hdbscan", "noise": True} for source_id in sorted(noise))
    communities.sort(key=lambda row: row["community_id"])
    return communities


def build_local_map(
    projection_rows: Sequence[Mapping[str, Any]], *, cache_dir: Path | str,
    options: LocalMappingOptions | Mapping[str, Any] | None = None,
    text_sources: Mapping[str, Mapping[str, Any]] | None = None,
    discovery_records: Sequence[Mapping[str, Any]] = (),
    citation_records: Sequence[Mapping[str, Any]] = (),
    progress: Callable[[Mapping[str, Any]], None] | None = None,
    encoder_factory: Callable[[LocalMappingOptions], Any] | None = None,
    before_commit: Callable[[dict[str, Any]], None] | None = None,
    calibration_report: Mapping[str, Any] | None = None,
    ranking_projection_rows: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build/refresh a private derived map without changing any source artifact.

    An injected encoder exposes tokenizer, dimension, identity and encode(texts).
    Production uses the offline local MiniLM adapter. Cache rows commit per batch;
    failure leaves the last complete result and validated reusable rows intact.
    """
    from .mapping_text import CLEANING_VERSION, prepare_embedding_spans

    options = options if isinstance(options, LocalMappingOptions) else LocalMappingOptions.from_mapping(options)
    options.validate()
    if isinstance(projection_rows, (str, bytes)) or len(projection_rows) > options.max_sources:
        raise ValueError("local mapping source count exceeds max_sources; reduce the explicit scope or raise the bounded limit")
    index = LocalRetrievalIndex(projection_rows, discovery_records, citation_records=citation_records, max_sources=options.max_sources)
    raw_rows = {row["source_id"]: row for row in projection_rows}
    text_sources = {} if text_sources is None else text_sources
    if not isinstance(text_sources, Mapping) or set(text_sources) - set(index.rows):
        raise ValueError("text sources require exact existing catalogue source IDs")
    root = Path(cache_dir).expanduser()
    if root.is_symlink():
        raise LocalMappingError("The derived mapping cache cannot be a symbolic link.")
    root.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    phases = defaultdict(float)
    cached_bytes = 0
    writes = 0

    def check_resources(extra: int = 0) -> None:
        if time.monotonic() - started > options.max_wall_seconds:
            raise LocalMappingError("Local mapping reached max_wall_seconds; committed chunks are preserved for explicit resume.")
        if shutil.disk_usage(root).free < options.min_free_disk_bytes + extra:
            raise LocalMappingError("Local mapping stopped at its free-disk limit; committed chunks are preserved.")
        if cached_bytes + writes + extra > options.max_cache_bytes:
            raise LocalMappingError("Local mapping cache size would exceed max_cache_bytes; no rows were silently omitted.")

    def emit(phase: str, **values: Any) -> None:
        if progress:
            progress({"phase": phase, **values})

    with _exclusive_cache(root):
        cached_bytes = _cache_size(root)
        check_resources()
        np = _numpy()
        _cluster_dependencies(options.clustering)  # Fail before encoder work for a missing selected backend.
        emit("preparing", sources=len(index.rows))
        prepared = []
        total_bytes = 0
        for source_id in sorted(index.rows):
            row = raw_rows[source_id]
            supplied = text_sources.get(source_id, {})
            if not isinstance(supplied, Mapping):
                raise ValueError("each text source must be an object")
            text = supplied.get("text", "")
            if not isinstance(text, str) or "\x00" in text:
                raise ValueError("source text must be a string without NUL characters")
            total_bytes += len(text.encode("utf-8"))
            if total_bytes > options.max_text_bytes:
                raise LocalMappingError("The explicit source text exceeds max_text_bytes; no sampling or silent truncation was performed.")
            if supplied.get("source_id", source_id) != source_id or (supplied.get("note_id") and supplied.get("note_id") != row.get("note_id")):
                raise ValueError("text source ownership differs from its catalogue binding")
            identity = dict(zip(_IDENTITY_FIELDS, index.identities[source_id])) if source_id in index.identities else {"scope": "workspace", "workspace_sha256": sha256_text(str(root.resolve())), "source_id": source_id}
            if supplied.get("identity") is not None and supplied["identity"] != identity:
                raise ValueError("text source crosses its explicit library/item identity")
            clean = prepare_embedding_spans(text, ownership_spans=supplied.get("ownership_spans", ()))
            full_hash = sha256_text(text)
            if supplied.get("content_sha256") and supplied["content_sha256"] != full_hash:
                raise ValueError("supplied source text hash does not match its content")
            source = {key: row.get(key, supplied.get(key, "")) for key in ("source_id", "note_id", "note_path", "note_hash", "profile_hash", "projection_hash", "profile_path", "profile_file_sha256", "metadata_path", "metadata_file_sha256")}
            source["note_content_sha256"] = str(supplied.get("note_content_sha256") or source["projection_hash"])
            source.update(identity=identity, full_text_sha256=full_hash,
                          input_representation=str(supplied.get("text_origin") or supplied.get("source_scope") or "unavailable"),
                          coverage={"input": dict(supplied.get("coverage") or {}), "cleaning": clean["coverage"],
                                    "status": "included" if clean["spans"] else ("excluded" if text.strip() else "unavailable")},
                          chunk_ids=[], embedding_text_sha256=_digest([span["text"] for span in clean["spans"]]))
            prepared.append((source, text, clean["spans"]))
        phases["preparation_seconds"] += time.monotonic() - started
        active = [entry for entry in prepared if entry[2]]
        # No model load is needed to report an empty or unavailable scope.
        model_setup_started = time.monotonic()
        bundle = (encoder_factory(options) if encoder_factory else _LocalMiniLM(options, root)) if active else None
        phases["model_setup_seconds"] = time.monotonic() - model_setup_started
        encoder_identity = dict(bundle.identity) if bundle else {"model_name": MODEL_NAME, "dimension": 384, "status": "not_loaded_empty_scope"}
        dimension = getattr(bundle, "dimension", 384)
        if type(dimension) is not int or not 1 <= dimension <= 4096 or encoder_identity.get("dimension") != dimension:
            raise LocalMappingError("Encoder dimension and its persisted identity disagree.")
        scope = _digest({source["source_id"]: source["identity"] for source, _, _ in prepared})
        # A source addition must not invalidate vectors for existing sources.
        embedding = {"encoder": encoder_identity, "cleaning_version": CLEANING_VERSION,
                     "chunking_version": "tokenizer-offset-blocks-v1",
                     "chunk_tokens": options.chunk_tokens, "aggregation": "mean_l2_chunk_vectors_then_l2"}
        embedding_digest = _digest(embedding)
        identity = {**embedding, "library_scope_sha256": scope,
                    "clustering": {name: getattr(options, name) for name in ("clustering", "neighbors", "min_similarity", "seed", "resolution", "min_cluster_size")}}
        identity["clustering"]["library_versions"] = _versions(options.clustering)
        identity["digest"] = _digest(identity)
        vector_dir = root / "chunks" / embedding_digest
        vector_dir.mkdir(parents=True, exist_ok=True)
        source_dir = root / "texts"
        source_dir.mkdir(exist_ok=True)
        pending = []
        vectors: dict[str, Any] = {}
        chunks = []
        reused = 0
        tokenization_started = time.monotonic()
        for source, text, spans in prepared:
            check_resources()
            full_path = source_dir / (_digest({"identity": source["identity"], "text_sha256": source["full_text_sha256"]}) + ".txt")
            encoded = text.encode("utf-8")
            if full_path.exists():
                if full_path.stat().st_size != len(encoded) or sha256_file(full_path) != source["full_text_sha256"]:
                    raise LocalMappingError("The preserved full-text cache failed its source checksum; the existing map and checkpoints were preserved.")
            else:
                check_resources(len(encoded))
                atomic_write_bytes(full_path, encoded)
                writes += len(encoded)
            source["text_cache_file"] = str(full_path.relative_to(root))
            occurrences: dict[str, int] = defaultdict(int)
            for span in spans:
                span_start, span_end = span["start"], span["end"]
                if type(span_start) is not int or type(span_end) is not int or text[span_start:span_end] != span["text"]:
                    raise ValueError("cleaning spans must retain exact source offsets and text")
                for left, right, piece in _token_chunks(span["text"], span_start, bundle.tokenizer, options.chunk_tokens):
                    owner = span.get("owner", "")
                    content = {"source_id": source["source_id"], "identity": source["identity"], "owner": owner,
                               "section_kind": span.get("section_kind", "unknown"), "text_sha256": sha256_text(piece)}
                    occurrence_key = _digest(content)
                    content["occurrence"] = occurrences[occurrence_key]
                    occurrences[occurrence_key] += 1
                    chunk_id = _digest({"embedding_identity": embedding_digest, **content})
                    chunk = {"chunk_id": chunk_id, **content, "start": left, "end": right}
                    chunks.append(chunk)
                    if len(chunks) > options.max_chunks:
                        raise LocalMappingError("Tokenized source content exceeds max_chunks; no partial sample was substituted.")
                    source["chunk_ids"].append(chunk_id)
                    path = vector_dir / (chunk_id + ".json")
                    try:
                        cached = json.loads(path.read_text(encoding="utf-8"))
                        binary = (vector_dir / (chunk_id + ".f32")).read_bytes()
                        if cached.get("chunk_id") != chunk_id or cached.get("embedding_identity") != embedding_digest or cached.get("vector_sha256") != hashlib.sha256(binary).hexdigest() or len(binary) != dimension * 4:
                            raise ValueError("checkpoint identity or checksum differs")
                        vector = np.frombuffer(binary, dtype="<f4").copy()
                        if not np.isfinite(vector).all() or abs(float(np.linalg.norm(vector)) - 1) > 1e-4:
                            raise ValueError("checkpoint vector is not finite and unit-normalized")
                    except FileNotFoundError:
                        pending.append((chunk, piece))
                    except (ValueError, OSError) as exc:
                        raise LocalMappingError(f"Invalid mapping checkpoint {chunk_id}; progress is preserved and no stale row was reused.") from exc
                    else:
                        vectors[chunk_id] = vector
                        reused += 1
        phases["tokenization_and_cache_seconds"] = time.monotonic() - tokenization_started
        emit("embedding", chunks=len(chunks), reused_chunks=reused, pending_chunks=len(pending))
        for offset in range(0, len(pending), options.batch_size):
            check_resources()
            batch = pending[offset:offset + options.batch_size]
            batch_started = time.monotonic()
            load_before = float(getattr(bundle, "model_load_seconds", 0.0))
            encoded = _unit_vectors(bundle.encode([piece for _, piece in batch]), len(batch), dimension)
            load_delta = float(getattr(bundle, "model_load_seconds", 0.0)) - load_before
            phases["embedding_seconds"] += max(0.0, time.monotonic() - batch_started - load_delta)
            check_resources()
            checkpoint_started = time.monotonic()
            for (chunk, _), vector in zip(batch, encoded, strict=True):
                binary = vector.astype("<f4", copy=False).tobytes()
                receipt = {"chunk_id": chunk["chunk_id"], "embedding_identity": embedding_digest,
                           "vector_sha256": hashlib.sha256(binary).hexdigest()}
                check_resources(len(binary) + len(_json(receipt).encode("utf-8")) + 128)
                atomic_write_bytes(vector_dir / (chunk["chunk_id"] + ".f32"), binary)
                write_json(vector_dir / (chunk["chunk_id"] + ".json"), receipt)
                writes += len(binary) + len(_json(receipt).encode("utf-8")) + 128
                vectors[chunk["chunk_id"]] = vector
            phases["checkpoint_seconds"] += time.monotonic() - checkpoint_started
            emit("embedding", completed_chunks=reused + min(offset + len(batch), len(pending)), total_chunks=len(chunks))
        aggregation_started = time.monotonic()
        ids, means = [], []
        for source, _, _ in prepared:
            if source["chunk_ids"]:
                mean = np.mean(np.stack([vectors[chunk_id] for chunk_id in source["chunk_ids"]]), axis=0)
                norm = float(np.linalg.norm(mean))
                if norm <= 1e-12:
                    raise LocalMappingError("A source's aggregated semantic vector has zero norm.")
                ids.append(source["source_id"])
                means.append(mean / norm)
        check_resources()
        tuning = {"mode": options.tuning_mode, "status": "manual" if options.tuning_mode == "manual" else "awaiting_evaluation_gate"}
        if options.tuning_mode == "auto" and calibration_report is not None:
            from .mapping_calibration import admitted_parameters, source_fingerprint, ranking_fingerprint
            active_ids = set(ids)
            ranking_rows = projection_rows if ranking_projection_rows is None else ranking_projection_rows
            ranking_rows = [row for row in ranking_rows if row.get("source_id") in active_ids]
            ranking_discovery = [row for row in discovery_records if row.get("source_id") in active_ids]
            ranking_citations = [row for row in citation_records if row.get("source_id") in active_ids and row.get("target_source_id") in active_ids]
            selected = admitted_parameters(calibration_report, embedding_identity=embedding_digest,
                                           source_ids=ids, source_fingerprint=source_fingerprint([source for source, _, _ in prepared]),
                                           ranking_inputs_sha256=ranking_fingerprint(ranking_rows, ranking_discovery, ranking_citations))
            if selected is not None:
                options = replace(options, **selected)
                tuning.update(status="calibrated", calibration_identity=calibration_report["calibration_identity"],
                              selected_parameters=selected, coverage=calibration_report["coverage"],
                              limitations=calibration_report["limitations"])
            else:
                tuning["status"] = "evaluation_gate_not_passed"
        identity["clustering"].update({name: getattr(options, name) for name in ("clustering", "neighbors", "min_similarity", "seed", "resolution", "min_cluster_size")})
        identity.pop("digest", None)
        identity["digest"] = _digest(identity)
        emit("clustering", algorithm=options.clustering, sources=len(ids))
        cluster_started = time.monotonic()
        matrix = np.stack(means) if means else np.empty((0, dimension))
        candidate_neighbors = _ranked_neighbors(ids, matrix, options=replace(options, neighbors=64, min_similarity=0.),
                                                embedding_identity=embedding_digest)
        counts: dict[str, int] = defaultdict(int)
        neighbors = []
        for neighbor in candidate_neighbors:
            sid = neighbor["source_id"]
            if neighbor["score"] > options.min_similarity and counts[sid] < options.neighbors:
                neighbors.append({**neighbor, "provenance": dict(neighbor["provenance"])})
                counts[sid] += 1
        # Explicit manual settings above 64 retain their historical graph policy.
        if options.neighbors > 64:
            neighbors = _ranked_neighbors(ids, matrix, options=options, embedding_identity=embedding_digest)
        if tuning["status"] == "awaiting_evaluation_gate" and options.clustering == "leiden":
            from .mapping_calibration import community_resolution_checks
            stability = community_resolution_checks(ids, neighbors, check_resources=check_resources)
            options = replace(options, resolution=stability["selected_resolution"], seed=stability["selected_seed"])
            tuning.update(status="default_policy_local_stability_checked", selected_parameters={
                "neighbors": options.neighbors, "min_similarity": options.min_similarity,
                "resolution": options.resolution, "seed": options.seed},
                community_checks=stability["community_scales"], limitations=stability["limitations"])
            identity["clustering"].update(resolution=options.resolution, seed=options.seed)
            identity.pop("digest", None)
            identity["digest"] = _digest(identity)
        communities = _communities_from_neighbors(ids, matrix, neighbors, options=options)
        community_scales = []
        if tuning["status"] in {"calibrated", "default_policy_local_stability_checked"} and options.clustering == "leiden":
            for scale, resolution in (("broad", .5), ("default", options.resolution), ("fine", 2.)):
                check_resources()
                scale_communities = communities if resolution == options.resolution else _communities_from_neighbors(
                    ids, matrix, neighbors, options=replace(options, resolution=resolution))
                community_scales.append({"scale": scale, "resolution": resolution, "communities": scale_communities})
        from .community_naming import deterministic_community_label
        for group in [communities, *[view["communities"] for view in community_scales]]:
            for community in group:
                members = [{"source_id": sid, "title": str(index.rows[sid].get("title") or sid)[:2000]}
                           for sid in community["source_ids"]]
                community["display_label"] = deterministic_community_label(members)
                community["display_label_kind"] = "deterministic_terms"
        phases["clustering_seconds"] = time.monotonic() - cluster_started
        phases["aggregation_seconds"] = cluster_started - aggregation_started
        check_resources()
        by_source = {source["source_id"]: source for source, _, _ in prepared}
        for neighbor in neighbors:
            neighbor["provenance"].update(source_chunk_ids=by_source[neighbor["source_id"]]["chunk_ids"],
                                           target_chunk_ids=by_source[neighbor["target_source_id"]]["chunk_ids"])
        result = {"schema_version": SCHEMA_VERSION, "identity": identity,
                  "sources": [source for source, _, _ in prepared], "chunks": chunks,
                  "communities": communities, "neighbors": neighbors, "candidate_neighbors": candidate_neighbors, "tuning": tuning, "community_scales": community_scales,
                  "stats": {"sources": len(prepared), "included_sources": len(ids), "chunks": len(chunks),
                            "encoded_chunks": len(pending), "reused_chunks": reused,
                            "model_load_seconds": float(getattr(bundle, "model_load_seconds", 0.0)),
                            **dict(phases), "total_seconds": time.monotonic() - started,
                            "input_text_bytes": total_bytes, "new_cache_bytes_upper_bound": writes},
                  "relationship_status": "derived_suggestions_only"}
        if before_commit:
            before_commit(result)
        serialized = _json(result) + "\n"
        check_resources(len(serialized.encode("utf-8")))
        atomic_write_text(root / "result.json", serialized)
        emit("complete", **result["stats"])
        return result
