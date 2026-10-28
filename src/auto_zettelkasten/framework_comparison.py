"""Opt-in, provider-neutral framework experiments with explicit spend admission.

Nothing imports a framework, downloads assets, or dispatches inference on import.
Native clients must be configured externally with pinned models and instrumented
provider transports. Offline bridge tests are not framework execution evidence.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal
import hashlib
import importlib
import inspect
import json
from pathlib import Path
import sqlite3
from typing import Any


class ComparisonError(ValueError):
    """An experimental contract cannot safely be evaluated."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FrameworkSpec:
    identity: str
    repository: str
    commit: str
    module: str
    modes: tuple[str, ...]
    stages: tuple[str, ...]
    source_url: str
    verified_on: str = "2026-10-26"


FRAMEWORKS = {
    "graphrag": FrameworkSpec(
        "graphrag", "microsoft/graphrag", "c600ff80b77b9b62c47d539a3ac9569e95d8949b",
        "graphrag.api", ("local", "global"),
        ("extraction", "description_summaries", "community_reports", "derived_embeddings", "query"),
        "https://github.com/microsoft/graphrag"),
    "lightrag": FrameworkSpec(
        "lightrag", "HKUDS/LightRAG", "aaebfe1bb8d55deb6a4304d357a3bf4c92fcd40a",
        "lightrag", ("mix", "naive"),
        ("extraction", "merge_summaries", "derived_embeddings", "query_keywords", "query"),
        "https://github.com/HKUDS/LightRAG"),
    "hipporag2": FrameworkSpec(
        "hipporag2", "OSU-NLP-Group/HippoRAG", "d5c8329422e0a0b834a15874545cb6a74b4f9b26",
        "hipporag", ("native", "dense"),
        ("triple_extraction", "derived_embeddings", "query_filter", "query"),
        "https://github.com/OSU-NLP-Group/HippoRAG"),
    "graphiti": FrameworkSpec(
        "graphiti", "getzep/graphiti", "5c27edfa8d3e0f49b2571bcebb4261bcc89ee037",
        "graphiti_core", ("hybrid",),
        ("fact_extraction", "entity_reconciliation", "derived_embeddings", "query"),
        "https://github.com/getzep/graphiti"),
    "llamaindex": FrameworkSpec(
        "llamaindex", "run-llama/llama_index", "619d74664e8404612038bc15a1a40c686037277c",
        "llama_index.core", ("property_graph",),
        ("graph_extraction", "derived_embeddings", "query_synonyms", "query"),
        "https://github.com/run-llama/llama_index"),
}


def validate_corpus(corpus: Sequence[Mapping[str, Any]]) -> tuple[dict[str, dict[str, Any]], str]:
    """Bind all adapters to exactly the same note bytes and coverage."""
    if not corpus or len(corpus) > 100_000:
        raise ComparisonError("a nonempty bounded corpus is required")
    rows: dict[str, dict[str, Any]] = {}
    for raw in corpus:
        sid, text = raw.get("source_id"), raw.get("text")
        if not isinstance(sid, str) or not sid or sid in rows:
            raise ComparisonError("source identities must be unique nonempty strings")
        if not isinstance(text, str) or not text.strip() or len(text) > 2_000_000:
            raise ComparisonError("each source requires bounded substantive text")
        if raw.get("content_sha256") != _sha(text):
            raise ComparisonError("source text hash mismatch")
        rows[sid] = {"source_id": sid, "text": text, "title": str(raw.get("title", "")),
                     "content_sha256": raw["content_sha256"], "coverage": raw.get("coverage", {})}
    # Coverage and title participate: matching only IDs or dimensions is unsafe.
    identity = _sha(_json([rows[sid] for sid in sorted(rows)]))
    return rows, identity


def prepare_framework_plan(
    framework: str, corpus: Sequence[Mapping[str, Any]], *, experiment_root: Path,
    extraction_model: str = "gpt-6-luna", embedding_identity: str,
) -> dict[str, Any]:
    """Return a reviewable, nondispatching plan; never create an index."""
    spec = FRAMEWORKS.get(framework)
    if spec is None:
        raise ComparisonError("unknown framework")
    if not embedding_identity or not extraction_model:
        raise ComparisonError("explicit pinned model identities are required")
    _, corpus_hash = validate_corpus(corpus)
    root = Path(experiment_root).resolve()
    return {
        "schema": "framework-comparison-plan-v1", "framework": asdict(spec),
        "corpus_sha256": corpus_hash, "source_count": len(corpus),
        "text_bytes": sum(len(row["text"].encode("utf-8")) for row in corpus),
        "text_characters": sum(len(row["text"]) for row in corpus),
        "extraction_model": extraction_model, "embedding_identity": embedding_identity,
        "working_directory": str(root / framework / "index"),
        "native_results_directory": str(root / framework / "native-results"),
        "provider_calls": 0, "paid_dispatch_enabled": False,
        "retry_count": 0, "fallback_models": [], "telemetry_enabled": False,
        "status": "prepared_not_executed", "runtime_verified": False,
        "cost_preview": {
            "status": "blocked_pending_bounded_stage_rates_and_native_transport_validation",
            "index_reservation_usd": None, "query_reservation_usd": None,
            "downstream_requests_known_in_advance": False,
            "required_stages": list(spec.stages),
            "token_counts": None, "characters_are_tokens": False,
        },
        "resources": {"dependency_download_bytes": None, "model_download_bytes": None,
                      "peak_memory_bytes": None, "index_bytes": None,
                      "measurement_status": "not_installed_or_run"},
        "requirements": [
            "Verify installed upstream commit before running.",
            "Inject pinned models and guarded provider transports; no global provider defaults.",
            "Disable framework and SDK automatic retries, fallbacks and telemetry.",
            "Admit each downstream call before dispatch; preserve unknown reservations.",
            "Do not treat newer scholarly claims as superseding older disagreement.",
        ],
    }


def _money(value: str | Decimal) -> Decimal:
    try:
        amount = Decimal(value)
    except Exception as exc:
        raise ComparisonError("invalid USD amount") from exc
    if not amount.is_finite() or amount < 0:
        raise ComparisonError("USD amount must be finite and nonnegative")
    return amount


@dataclass(frozen=True)
class StageBound:
    """A conditional model-usage reservation, not an invoice guarantee."""

    framework: str
    stage: str
    max_calls: int
    max_input_tokens_per_call: int
    max_output_tokens_per_call: int
    input_usd_per_token: str
    output_usd_per_token: str
    pricing_source: str
    model: str

    def reservation(self) -> Decimal:
        if self.framework not in FRAMEWORKS or self.stage not in FRAMEWORKS[self.framework].stages:
            raise ComparisonError("unknown framework stage")
        for value in (self.max_calls, self.max_input_tokens_per_call, self.max_output_tokens_per_call):
            if type(value) is not int or value < 0:
                raise ComparisonError("stage call and token bounds must be nonnegative integers")
        if not self.pricing_source.startswith("https://") or not self.model:
            raise ComparisonError("pinned model and documented pricing source are required")
        return self.max_calls * (self.max_input_tokens_per_call * _money(self.input_usd_per_token)
                                 + self.max_output_tokens_per_call * _money(self.output_usd_per_token))


def preview_stage_bounds(bounds: Sequence[StageBound]) -> dict[str, Any]:
    """Price explicit proposed limits without claiming actual calls or tokens.

    Downstream extraction is adaptive. These limits deliberately stop a stage
    rather than promising its completion if the framework needs more work.
    """
    if not bounds or len({(b.framework, b.stage) for b in bounds}) != len(bounds):
        raise ComparisonError("distinct bounded stages are required")
    stages, total, per_framework = [], Decimal(0), {}
    for bound in bounds:
        amount = bound.reservation()
        total += amount
        per_framework[bound.framework] = per_framework.get(bound.framework, Decimal(0)) + amount
        stages.append({**asdict(bound), "reservation_usd": str(amount),
                       "max_input_tokens": bound.max_calls * bound.max_input_tokens_per_call,
                       "max_output_tokens": bound.max_calls * bound.max_output_tokens_per_call})
    return {"schema": "framework-stage-preview-v1", "currency": "USD",
            "aggregate_reservation_usd": str(total),
            "framework_reservations_usd": {k: str(v) for k, v in sorted(per_framework.items())},
            "stages": stages, "provider_calls": 0, "dispatch_approved": False,
            "guaranteed_invoice_cap": False, "completion_guaranteed": False,
            "bound_scope": "proposed_explicit_standard_model_usage_limits",
            "blocked_until": ["provider instrumentation enforces every call and token limit",
                              "framework installation/compatibility and retry/fallback audits pass",
                              "exact corpus and stage limits receive separate paid authorization"],
            "exclusions": ["taxes", "regional or speed premiums", "unapproved model changes",
                           "local memory/disk/electricity", "additional native final-answer runs"],
            "stop_when_cap_exhausted": True}


class AggregateSpendLedger:
    """Private explicit-approval ledger with atomic aggregate and per-system caps.

    Reservations are per bounded stage. Unknown outcomes remain reserved. A
    framework transport must independently enforce its declared per-call limits;
    reserving a stage alone does not instrument an untrusted native client.
    """

    def __init__(self, path: Path, *, approval_id: str, aggregate_usd: str,
                 framework_caps: Mapping[str, str]):
        if not approval_id or not framework_caps or any(k not in FRAMEWORKS for k in framework_caps):
            raise ComparisonError("explicit approval and framework sublimits are required")
        self.path, self.approval_id = Path(path), approval_id
        self.aggregate = _money(aggregate_usd)
        self.caps = {key: _money(value) for key, value in framework_caps.items()}
        if sum(self.caps.values(), Decimal(0)) > self.aggregate:
            raise ComparisonError("framework sublimits exceed aggregate approval")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS policy (id INTEGER PRIMARY KEY, identity TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS stages (request_id TEXT PRIMARY KEY, framework TEXT NOT NULL, amount TEXT NOT NULL, actual TEXT, status TEXT NOT NULL, bound_json TEXT NOT NULL)")
            identity = _json({"approval_id": approval_id, "aggregate": str(self.aggregate),
                              "caps": {k: str(v) for k, v in sorted(self.caps.items())}})
            old = db.execute("SELECT identity FROM policy WHERE id=1").fetchone()
            if old and old[0] != identity:
                raise ComparisonError("ledger approval identity cannot be changed")
            db.execute("INSERT OR IGNORE INTO policy VALUES (1, ?)", (identity,))

    def _db(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    def reserve(self, request_id: str, bound: StageBound) -> dict[str, Any]:
        if not request_id or len(request_id) > 256 or bound.framework not in self.caps:
            raise ComparisonError("unapproved request/framework")
        amount = bound.reservation()
        serialized = _json(asdict(bound))
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM stages WHERE request_id=?", (request_id,)).fetchone():
                raise ComparisonError("request already reserved; no implicit retry")
            rows = db.execute("SELECT framework, amount, actual, status FROM stages").fetchall()
            def exposure(row: tuple[Any, ...]) -> Decimal:
                return _money(row[2] if row[3] == "settled" else row[1])
            total = sum((exposure(row) for row in rows), Decimal(0))
            subtotal = sum((exposure(row) for row in rows if row[0] == bound.framework), Decimal(0))
            if total + amount > self.aggregate or subtotal + amount > self.caps[bound.framework]:
                raise ComparisonError("aggregate or framework spending limit exceeded")
            db.execute("INSERT INTO stages VALUES (?, ?, ?, NULL, 'reserved', ?)",
                       (request_id, bound.framework, str(amount), serialized))
        return {"request_id": request_id, "reservation_usd": str(amount), "status": "reserved"}

    def mark_unknown(self, request_id: str) -> None:
        with self._db() as db:
            result = db.execute("UPDATE stages SET status='unknown' WHERE request_id=? AND status='reserved'", (request_id,))
            if result.rowcount != 1:
                raise ComparisonError("only a reserved stage can become unknown")

    def settle(self, request_id: str, actual_usd: str) -> None:
        actual = _money(actual_usd)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT amount, status FROM stages WHERE request_id=?", (request_id,)).fetchone()
            if not row or row[1] not in ("reserved", "unknown") or actual > _money(row[0]):
                raise ComparisonError("settlement missing, repeated, or exceeds reservation")
            db.execute("UPDATE stages SET status='settled', actual=? WHERE request_id=?", (str(actual), request_id))

    def snapshot(self) -> dict[str, Any]:
        with self._db() as db:
            rows = db.execute("SELECT request_id, framework, amount, actual, status FROM stages ORDER BY request_id").fetchall()
        return {"approval_id": self.approval_id, "aggregate_usd": str(self.aggregate),
                "exposure_usd": str(sum((_money(r[3] if r[4] == "settled" else r[2]) for r in rows), Decimal(0))),
                "stages": [{"request_id": r[0], "framework": r[1], "reserved_usd": r[2],
                            "actual_usd": r[3], "status": r[4]} for r in rows]}


@dataclass(frozen=True)
class NativeHit:
    native_id: str
    text: str
    source_ids: tuple[str, ...] = ()
    evidence_quotes: tuple[tuple[str, str], ...] = ()
    kind: str = "passage"


def normalize_evidence(
    hits: Sequence[NativeHit], corpus: Sequence[Mapping[str, Any]], *, framework: str,
    corpus_sha256: str, max_notes: int = 8, max_chars: int = 12_000,
) -> dict[str, Any]:
    """Ground literal evidence; attribution alone never validates a paraphrase."""
    rows, actual_hash = validate_corpus(corpus)
    if framework not in FRAMEWORKS or corpus_sha256 != actual_hash:
        raise ComparisonError("framework or corpus identity mismatch")
    if type(max_notes) is not int or not 0 < max_notes <= 128 or type(max_chars) is not int or not 0 < max_chars <= 1_000_000:
        raise ComparisonError("invalid evidence budget")
    native, grounded, failures, seen = [], [], [], set()
    for hit in hits:
        if not isinstance(hit, NativeHit) or not hit.native_id or len(hit.text) > 2_000_000:
            raise ComparisonError("invalid native hit")
        record = asdict(hit)
        valid_ids = set(hit.source_ids) & rows.keys()
        unknown = set(hit.source_ids) - rows.keys()
        quotes = list(hit.evidence_quotes)
        if not quotes and hit.kind == "passage":
            # Exact text lookup is allowed only when it uniquely identifies a
            # source. Never infer identity from a title or a generated claim.
            matches = [sid for sid, row in rows.items() if hit.text and hit.text in row["text"]]
            if len(matches) == 1 and (not valid_ids or matches[0] in valid_ids):
                quotes = [(matches[0], hit.text)]
        # Reject the whole record before exposing any otherwise valid quote:
        # partial acceptance would launder mixed or forged attribution.
        if unknown or any(sid not in rows or not quote or quote not in rows[sid]["text"]
                          or (valid_ids and sid not in valid_ids) for sid, quote in quotes):
            record.update(grounding_status="invalid_attribution_or_quote", included_source_ids=[])
            native.append(record)
            failures.append({"native_id": hit.native_id, "grounding_status": "invalid_attribution_or_quote"})
            continue
        evidence = []
        invalid_quote = False
        for sid, quote in quotes:
            if sid not in rows or not quote or quote not in rows[sid]["text"] or (valid_ids and sid not in valid_ids):
                invalid_quote = True
                continue
            if sid in seen or len(grounded) >= max_notes:
                continue
            item = {"source_id": sid, "excerpt": quote, "content_sha256": rows[sid]["content_sha256"],
                    "coverage": rows[sid]["coverage"], "retrieval_route": framework,
                    "native_id": hit.native_id, "grounding_status": "literal_note_evidence"}
            if len(_json(grounded + [item])) <= max_chars:
                grounded.append(item)
                seen.add(sid)
                evidence.append(sid)
        status = ("invalid_attribution_or_quote" if unknown or invalid_quote else
                  "literal_note_evidence" if quotes else "source_trace_only" if valid_ids else "untraceable")
        record.update(grounding_status=status, included_source_ids=evidence)
        native.append(record)
        if status != "literal_note_evidence":
            failures.append({"native_id": hit.native_id, "grounding_status": status})
    return {"framework": framework, "corpus_sha256": actual_hash, "notes": grounded,
            "native_records": native, "grounding_failures": failures,
            "chars": len(_json(grounded)), "truncated": len(seen) < len({sid for h in hits for sid in h.source_ids}),
            "original_publication_verified": False}


async def _await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class NativeFrameworkAdapter:
    """Thin executable native API bridge, with no implicit client/model setup.

    Construction is safe. These entry points stay disabled because even
    retrieval can trigger inference. Controlled SDK factories and per-call
    metering are separate explicit interfaces; their verification is not paid
    authorization. Recorded replay never calls the supplied native client.
    """

    def __init__(self, framework: str, *, installed_commit: str, client: Any,
                 corpus_sha256: str):
        if framework not in FRAMEWORKS or installed_commit != FRAMEWORKS[framework].commit:
            raise ComparisonError("native adapter requires its audited upstream commit")
        if not isinstance(corpus_sha256, str) or len(corpus_sha256) != 64:
            raise ComparisonError("corpus identity required")
        self.spec, self.client, self.corpus_sha256 = FRAMEWORKS[framework], client, corpus_sha256

    async def query(self, *args: Any, **kwargs: Any) -> Any:
        raise ComparisonError("native dispatch unavailable here; an explicitly authorized metered experiment runner is required")

    async def build_index(self, *args: Any, **kwargs: Any) -> Any:
        raise ComparisonError("native dispatch unavailable here; an explicitly authorized metered experiment runner is required")

    def bind_graphiti_ingestion(self, native_results: Sequence[Any], *,
                               corpus: Sequence[Mapping[str, Any]], group_id: str) -> dict[str, Any]:
        """Bind actual returned episode UUIDs; never assume preselected IDs."""
        if self.spec.identity != "graphiti":
            raise ComparisonError("episode bindings require the Graphiti adapter")
        from .graphiti_grounding import bind_ingested_episodes
        return bind_ingested_episodes(native_results, source_ids=sorted(row["source_id"] for row in corpus),
                                     corpus=corpus, corpus_sha256=self.corpus_sha256, group_id=group_id)

    async def ground_graphiti_query(self, native_result: Sequence[Any], *,
                                    corpus: Sequence[Mapping[str, Any]], driver: Any,
                                    episode_bindings: Mapping[str, Any], query: str = "",
                                    fetch_episodes: Callable[..., Any] | None = None,
                                    max_notes: int = 8, max_chars: int = 12_000) -> dict[str, Any]:
        """Resolve saved/native fact hits to original notes without inference.

        Generated facts remain an audit trail, not verified quotations. The
        injected reader is a read-only episode lookup, never an LLM callback.
        """
        if self.spec.identity != "graphiti":
            raise ComparisonError("episode grounding requires the Graphiti adapter")
        from .graphiti_grounding import hydrate_graphiti_evidence
        return await hydrate_graphiti_evidence(native_result, driver=driver, bindings=episode_bindings,
                                              corpus=corpus, corpus_sha256=self.corpus_sha256, query=query,
                                              fetch_episodes=fetch_episodes, max_notes=max_notes, max_chars=max_chars)

    async def offline_replay_query(self, query: str, *, mode: str, recorded_response: Any,
                                   native_kwargs: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Exercise native API wiring against recorded data; never the client."""
        replay = _RecordedClient(recorded_response)
        result = await self._query_native(replay, query, mode=mode, native_kwargs=native_kwargs)
        return {"native_result": result, "native_calls": replay.calls, "provider_calls": 0,
                "execution_kind": "offline_recorded_replay"}

    async def offline_replay_index(self, *, corpus: Sequence[Mapping[str, Any]], recorded_response: Any,
                                   native_kwargs: Mapping[str, Any] | None = None) -> dict[str, Any]:
        replay = _RecordedClient(recorded_response)
        result = await self._build_native(replay, corpus=corpus, native_kwargs=native_kwargs)
        return {"native_result": result, "native_calls": replay.calls, "provider_calls": 0,
                "execution_kind": "offline_recorded_replay"}

    async def _query_native(self, client: Any, query: str, *, mode: str,
                            native_kwargs: Mapping[str, Any] | None = None) -> Any:
        if mode not in self.spec.modes or not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise ComparisonError("unsupported mode or invalid query")
        kwargs = dict(native_kwargs or {})
        if any(k in kwargs for k in ("query", "queries", "mode", "retries", "fallback_model")):
            raise ComparisonError("native kwargs cannot replace query, mode, retries or fallback policy")
        if self.spec.identity == "graphrag":
            return await _await(getattr(client, f"{mode}_search")(query=query, **kwargs))
        if self.spec.identity == "lightrag":
            # QueryParam creation stays explicit: mode and only_need_context
            # must be checked, avoiding default answer-generation calls.
            param = kwargs.pop("param", None)
            if getattr(param, "mode", None) != mode:
                raise ComparisonError("explicit matching LightRAG QueryParam required")
            return await _await(client.aquery_data(query, param=param, **kwargs))
        if self.spec.identity == "hipporag2":
            # Dense mode requires the native dense retriever, not a relabelled
            # graph result. The provided client must expose that control.
            method = "retrieve" if mode == "native" else "retrieve_dpr"
            return await _await(getattr(client, method)(queries=[query], **kwargs))
        if self.spec.identity == "graphiti":
            return await _await(client.search(query=query, **kwargs))
        return await _await(client.retrieve(query, **kwargs))

    async def _build_native(self, client: Any, *, corpus: Sequence[Mapping[str, Any]],
                            native_kwargs: Mapping[str, Any] | None = None) -> Any:
        rows, corpus_hash = validate_corpus(corpus)
        if corpus_hash != self.corpus_sha256:
            raise ComparisonError("index corpus mismatch")
        kwargs = dict(native_kwargs or {})
        ordered = [rows[sid] for sid in sorted(rows)]
        if self.spec.identity == "graphrag":
            documents = kwargs.get("input_documents")
            if "config" not in kwargs or not hasattr(documents, "to_dict"):
                raise ComparisonError("explicit GraphRAG config and input_documents required")
            observed = [(r.get("id"), r.get("text")) for r in documents.to_dict("records")]
            if sorted(observed) != sorted((r["source_id"], r["text"]) for r in ordered):
                raise ComparisonError("GraphRAG input_documents differ from common corpus")
            return await _await(client.build_index(**kwargs))
        if self.spec.identity == "lightrag":
            if set(kwargs) & {"input", "ids", "file_paths"}:
                raise ComparisonError("cannot replace frozen LightRAG documents")
            return await _await(client.ainsert([r["text"] for r in ordered],
                                                  ids=[r["source_id"] for r in ordered], **kwargs))
        if self.spec.identity == "hipporag2":
            if "docs" in kwargs:
                raise ComparisonError("cannot replace frozen HippoRAG documents")
            return await _await(client.index(docs=[r["text"] for r in ordered], **kwargs))
        if self.spec.identity == "graphiti":
            # The caller supplies a neutral fixed ingestion reference_time;
            # publication date must not turn disagreement into supersession.
            if "reference_time" not in kwargs:
                raise ComparisonError("fixed Graphiti ingestion reference_time required")
            if not isinstance(kwargs.get("group_id"), str) or not kwargs["group_id"].strip():
                raise ComparisonError("isolated Graphiti episode group required")
            if getattr(kwargs.get("source"), "value", kwargs.get("source")) != "text":
                raise ComparisonError("Graphiti episodes must use the explicit text source type")
            if set(kwargs) & {"name", "episode_body", "source_description", "uuid"}:
                raise ComparisonError("cannot replace frozen Graphiti episodes")
            return [await _await(client.add_episode(name=r["source_id"], episode_body=r["text"],
                        source_description="frozen research note; scholarly claims may disagree", **kwargs)) for r in ordered]
        # An explicitly configured PropertyGraphIndex class/factory and Document
        # objects avoid llama-index global Settings creating implicit clients.
        documents = kwargs.pop("documents", None)
        if documents is None or "llm" not in kwargs or "embed_model" not in kwargs:
            raise ComparisonError("explicit LlamaIndex documents, llm and embed_model required")
        observed = [(getattr(d, "doc_id", None), getattr(d, "text", None)) for d in documents]
        if sorted(observed) != sorted((r["source_id"], r["text"]) for r in ordered):
            raise ComparisonError("LlamaIndex documents differ from common corpus")
        return await _await(client.from_documents(documents, **kwargs))


class _RecordedClient:
    """No network-capable SDK is reachable from the replay path."""

    def __init__(self, response: Any):
        self.response, self.calls = response, []

    def __getattr__(self, method: str) -> Callable[..., Any]:
        async def replay(*args: Any, **kwargs: Any) -> Any:
            self.calls.append({"method": method, "args": args, "kwargs": kwargs})
            return self.response
        return replay


def load_native_module(framework: str) -> Any:
    """Explicit optional import only; callers still verify the installed commit."""
    if framework not in FRAMEWORKS:
        raise ComparisonError("unknown framework")
    return importlib.import_module(FRAMEWORKS[framework].module)


def extract_native_hits(framework: str, result: Any, *, source_bindings: Mapping[str, Sequence[str]]) -> list[NativeHit]:
    """Decode native evidence records without inventing source identities.

    ``source_bindings`` must come from the frozen index's ingestion mapping.
    Graph fact attribution remains trace-only until literal evidence is supplied.
    Native synthesis answers are deliberately not treated as source passages.
    """
    if framework not in FRAMEWORKS:
        raise ComparisonError("unknown framework")
    def get(obj: Any, key: str, default: Any = None) -> Any:
        return obj.get(key, default) if isinstance(obj, Mapping) else getattr(obj, key, default)
    def bound(identity: str) -> tuple[str, ...]:
        values = source_bindings.get(identity, ())
        if isinstance(values, str) or not all(isinstance(v, str) for v in values):
            raise ComparisonError("source bindings must contain source identity lists")
        return tuple(values)
    hits = []
    if framework == "graphrag":
        context = result[1] if isinstance(result, tuple) and len(result) == 2 else result
        if not isinstance(context, Mapping):
            raise ComparisonError("GraphRAG must expose context data separately from its answer")
        sources = context.get("sources", ())
        records = sources.to_dict("records") if hasattr(sources, "to_dict") else sources
        for i, row in enumerate(records):
            identity = str(get(row, "id", i))
            hits.append(NativeHit(identity, str(get(row, "text", "")), bound(identity)))
    elif framework == "lightrag":
        if not isinstance(result, Mapping) or result.get("status") != "success":
            raise ComparisonError("LightRAG did not return successful structured retrieval data")
        for i, row in enumerate(result.get("data", {}).get("chunks", ())):
            identity = str(get(row, "chunk_id", get(row, "id", i)))
            hits.append(NativeHit(identity, str(get(row, "content", "")), bound(identity)))
    elif framework == "hipporag2":
        solutions = result[0] if isinstance(result, tuple) else result
        if not isinstance(solutions, Sequence) or isinstance(solutions, (str, bytes)):
            raise ComparisonError("HippoRAG must return query solutions")
        for solution in solutions:
            for i, text in enumerate(get(solution, "docs", ())):
                identity = _sha(str(text))
                hits.append(NativeHit(identity, str(text), bound(identity)))
    elif framework == "graphiti":
        for edge in result:
            identity = str(get(edge, "uuid", ""))
            ids = set(bound(identity))
            for episode in get(edge, "episodes", ()):
                ids.update(bound(str(episode)))
            hits.append(NativeHit(identity, str(get(edge, "fact", "")), tuple(sorted(ids)), kind="fact"))
    else:
        for row in result:
            node = get(row, "node", row)
            identity = str(get(node, "node_id", ""))
            text = node.get_content() if hasattr(node, "get_content") else get(node, "text", "")
            ids = set(bound(identity))
            ref = get(node, "ref_doc_id", None)
            if ref:
                ids.update(bound(str(ref)))
            hits.append(NativeHit(identity, str(text), tuple(sorted(ids))))
    return hits
