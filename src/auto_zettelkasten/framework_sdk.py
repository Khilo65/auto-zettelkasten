"""Explicit SDK compatibility and injection seams for private comparisons.

Optional SDKs are imported only by explicit calls. Version/signature checks do
not claim that a live index is verified; native runtime smoke evidence is kept
outside the repository. No provider client or credential is constructed here.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, is_dataclass
import hashlib
import importlib
import importlib.metadata
import inspect
import asyncio
import json
import marshal
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .framework_comparison import ComparisonError, FRAMEWORKS


@dataclass(frozen=True)
class SDKContract:
    distribution: str
    version: str
    module: str
    object_name: str | None
    injection_fields: tuple[str, ...]


CONTRACTS = {
    "graphrag": SDKContract("graphrag", "3.3.0", "graphrag.api", None, ("config", "input_documents")),
    "lightrag": SDKContract("lightrag-hku", "1.5.8", "lightrag", "LightRAG", ("working_dir", "llm_model_func", "embedding_func")),
    "hipporag2": SDKContract("hipporag", "2.0.0a5", "hipporag", "HippoRAG", ("extraction_llm", "qa_llm", "embedding_model", "index_identity")),
    "graphiti": SDKContract("graphiti-core", "0.31.2", "graphiti_core", "Graphiti", ("llm_client", "embedder", "cross_encoder", "graph_driver")),
    "llamaindex": SDKContract("llama-index-core", "0.14.25", "llama_index.core", "PropertyGraphIndex", ("llm", "embed_model", "kg_extractors")),
}


def verify_sdk_contract(framework: str, *, expected_commit: str) -> dict[str, Any]:
    """Inspect the installed native SDK, never substitute a different release."""
    if framework not in CONTRACTS or expected_commit != FRAMEWORKS[framework].commit:
        raise ComparisonError("SDK requires the frozen framework commit")
    contract = CONTRACTS[framework]
    installed = importlib.metadata.version(contract.distribution)
    if installed != contract.version:
        raise ComparisonError("installed SDK version differs from the audited source pin")
    module = importlib.import_module(contract.module)
    target = getattr(module, contract.object_name) if contract.object_name else module.build_index
    parameters = inspect.signature(target).parameters
    missing = set(contract.injection_fields) - parameters.keys()
    if missing:
        raise ComparisonError("native SDK lacks required injection fields: " + ", ".join(sorted(missing)))
    methods = ("build_index", "local_search", "global_search") if framework == "graphrag" else {
        "lightrag": ("ainsert", "aquery_data"), "hipporag2": ("index", "retrieve", "retrieve_dpr"),
        "graphiti": ("add_episode", "search"), "llamaindex": ("from_documents", "as_retriever"),
    }[framework]
    parent = module if framework == "graphrag" else target
    signatures = {}
    for name in methods:
        method = getattr(parent, name)
        signatures[name] = str(inspect.signature(method))
    return {"framework": framework, "commit": expected_commit, "distribution": contract.distribution,
            "version": installed, "module_file": str(Path(module.__file__).resolve()),
            "constructor_signature": str(inspect.signature(target)), "methods": signatures,
            "native_import_verified": True, "provider_calls": 0,
            "source_commit_attestation": "requires separate installed-file hash proof",
            "live_index_verified": False}


def verify_installed_files(installed_root: Path, expected_files: Mapping[str, str]) -> dict[str, str]:
    """Verify native package bytes against exact files from the frozen commit."""
    root = Path(installed_root).resolve()
    if not expected_files or len(expected_files) > 5000:
        raise ComparisonError("bounded native file identity manifest required")
    checked = {}
    for relative, digest in expected_files.items():
        name = Path(relative)
        path = (root / name).resolve()
        if name.is_absolute() or ".." in name.parts or not path.is_relative_to(root):
            raise ComparisonError("native file identity escapes package root")
        if not isinstance(digest, str) or len(digest) != 64 or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
            raise ComparisonError("invalid native file identity")
        with path.open("rb") as handle:
            observed = hashlib.file_digest(handle, "sha256").hexdigest()
        if observed != digest:
            raise ComparisonError("installed native file differs from frozen source")
        checked[relative] = observed
    return checked


def bind_native_call(callable_object: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Validate arguments against a real SDK signature without executing it."""
    return dict(inspect.signature(callable_object).bind(*args, **kwargs).arguments)


def comparison_execution_identity(framework: str, *, corpus_identity: str,
                                  embedding_identity: Mapping[str, Any], extraction_model: str = "gpt-6-luna",
                                  inference_config: Mapping[str, Any] | None = None,
                                  native_config: Mapping[str, Any] | None = None) -> str:
    """Separate native derived-index caches from canonical source embeddings."""
    required = {"encoder", "split_policy", "pool_policy", "dimension", "max_tokens", "query_instruction_policy"}
    if framework not in FRAMEWORKS or not isinstance(embedding_identity, Mapping) or not required <= embedding_identity.keys():
        raise ComparisonError("full derived embedding representation identity is required")
    if len(corpus_identity) != 64 or any(c not in "0123456789abcdef" for c in corpus_identity):
        raise ComparisonError("invalid corpus identity")
    if embedding_identity["dimension"] != 384 or embedding_identity["max_tokens"] != 256 or not embedding_identity["encoder"]:
        raise ComparisonError("derived embedding identity differs from the pinned representation")
    identity = {"schema": "framework-comparison-context-v1", "framework": framework,
                "framework_commit": FRAMEWORKS[framework].commit, "corpus_sha256": corpus_identity,
                "extraction_model": extraction_model, "embedding": dict(embedding_identity),
                "native_config": dict(native_config or {}), "native_state_policy": "fresh_only_v1",
                "inference_config": dict(inference_config or {"max_output_tokens": 4096, "reasoning_effort": "none"})}
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded) > 128_000:
        raise ComparisonError("execution identity exceeds bounded metadata")
    return hashlib.sha256(encoded.encode()).hexdigest()


def _configuration_value(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _configuration_value(item) for key, item in value.items()
                if str(key).lower() not in {"api_key", "password", "secret", "access_token", "save_dir"}}
    if isinstance(value, (list, tuple)):
        return [_configuration_value(item) for item in value]
    if callable(value) and hasattr(value, "__code__"):
        return {"callable": value.__module__ + "." + value.__qualname__,
                "code_sha256": hashlib.sha256(marshal.dumps(value.__code__)).hexdigest()}
    if is_dataclass(value):
        return _configuration_value(asdict(value))
    name = type(value).__module__ + "." + type(value).__qualname__
    if hasattr(value, "model_dump"):
        return {"type": name, "settings": _configuration_value(value.model_dump(mode="python")),
                "tokenizer": _configuration_value(getattr(value, "_tokenizer", None))}
    return {"type": name}


def native_configuration_descriptor(framework: str, components: Mapping[str, Any]) -> dict[str, Any]:
    """Freeze relevant actual component settings; first pilot never reuses state."""
    if framework == "hipporag2":
        config = components.get("config")
        if not is_dataclass(config):
            raise ComparisonError("HippoRAG requires its actual dataclass configuration")
        raw = asdict(config)
        if raw.get("openie_mode") != "online":
            raise ComparisonError("only the injected online OpenIE protocol is admitted")
        raw.update(llm_name="gpt-6-luna", embedding_model_name="sentence-transformers/all-MiniLM-L6-v2")
        return {"framework": framework, "config": _configuration_value(raw)}
    fields = {"lightrag": ("tokenizer",), "graphiti": ("graph_driver", "cross_encoder"),
              "llamaindex": ("kg_extractors", "transformations", "property_graph_store")}.get(framework)
    if fields is None:
        raise ComparisonError("native API configuration must be frozen explicitly")
    return {"framework": framework, "components": {name: _configuration_value(components.get(name)) for name in fields},
            "native_state_policy": "fresh_only_v1", "controlled_factory_version": "sdk-injection-v2"}


class _DisabledReranker:
    async def rank(self, query, passages):
        raise ComparisonError("model-based Graphiti reranking is disabled; use the RRF search recipe")


def make_disabled_graphiti_reranker():
    from graphiti_core.cross_encoder.client import CrossEncoderClient
    class DisabledReranker(_DisabledReranker, CrossEncoderClient):
        pass
    return DisabledReranker()


def construct_injected_client(framework: str, *, components: Mapping[str, Any],
                              working_directory: Path, corpus_identity: str,
                              extraction_model: str = "gpt-6-luna",
                              embedding_identity: Mapping[str, Any] | None = None,
                              inference_config: Mapping[str, Any] | None = None) -> Any:
    """Construct a native client using caller-supplied controlled components.

    This is an injection boundary, not paid authorization. The parent must use
    metered transports and test the native hooks in a network-denied subprocess.
    GraphRAG's provider registry is configured separately because its API takes
    explicit config and table inputs rather than a persistent client instance.
    """
    if framework not in CONTRACTS or framework == "graphrag":
        raise ComparisonError("framework requires explicit native API configuration")
    if extraction_model != "gpt-6-luna" or not isinstance(corpus_identity, str) or len(corpus_identity) != 64:
        raise ComparisonError("frozen model and corpus identity required")
    if inference_config is not None and dict(inference_config) != {"max_output_tokens": 4096, "reasoning_effort": "none"}:
        raise ComparisonError("unsupported native inference settings; factory uses the frozen controlled defaults")
    root = Path(working_directory).resolve()
    if any((parent / ".git").exists() for parent in (root, *root.parents)):
        raise ComparisonError("native comparison state must remain outside Git")
    requirements = {
        "lightrag": ("completion", "embedding", "tokenizer"),
        "hipporag2": ("extraction_llm", "qa_llm", "embedding_model", "config"),
        "graphiti": ("llm_client", "embedder", "cross_encoder", "graph_driver"),
        "llamaindex": ("llm", "embed_model", "kg_extractors", "property_graph_store", "transformations"),
    }[framework]
    if any(components.get(name) is None for name in requirements):
        raise ComparisonError("every native inference and storage component must be supplied")
    execution_sha = comparison_execution_identity(framework, corpus_identity=corpus_identity,
                                                embedding_identity=embedding_identity,
                                                extraction_model=extraction_model, inference_config=inference_config,
                                                native_config=native_configuration_descriptor(framework, components))
    embedding_component = components.get({"lightrag": "embedding", "hipporag2": "embedding_model",
                                         "graphiti": "embedder", "llamaindex": "embed_model"}[framework])
    expected_repr = hashlib.sha256(json.dumps(embedding_identity, sort_keys=True, ensure_ascii=False,
                                             separators=(",", ":")).encode()).hexdigest()
    if getattr(embedding_component, "comparison_representation_sha256", None) != expected_repr:
        raise ComparisonError("native embedding component does not bind the declared representation")
    from .framework_embeddings import DerivedEmbeddingTransport
    if not isinstance(getattr(embedding_component, "_comparison_embedding_hook", None), DerivedEmbeddingTransport):
        raise ComparisonError("native embedding component requires the guarded local transport adapter")
    llm_fields = {"lightrag": ("completion",), "hipporag2": ("extraction_llm", "qa_llm"),
                  "graphiti": ("llm_client",), "llamaindex": ("llm",)}[framework]
    for name in llm_fields:
        value = components[name]
        if not isinstance(getattr(value, "_comparison_hook", getattr(value, "__self__", None)), MeteredSDKChat):
            raise ComparisonError("native inference components require a metered SDK adapter")
    if framework == "graphiti" and not isinstance(components["cross_encoder"], _DisabledReranker):
        raise ComparisonError("controlled Graphiti search requires disabled model reranking")
    for component in components.values():
        hook = getattr(component, "_comparison_hook", getattr(component, "__self__", None))
        if isinstance(hook, MeteredSDKChat):
            if hook.framework != framework:
                raise ComparisonError("native model callback framework identity mismatch")
            approval = hook.transport.ledger.approval
            if approval is not None and approval.corpus_sha256 != corpus_identity:
                raise ComparisonError("native model callback corpus differs from spending approval")
            if approval is not None and approval.context_sha256 != execution_sha:
                raise ComparisonError("native model callback execution differs from spending approval")
    root = root / execution_sha
    if root.exists():
        raise ComparisonError("first controlled pilot requires fresh native state; reuse is not admitted")
    verify_sdk_contract(framework, expected_commit=FRAMEWORKS[framework].commit)
    module = importlib.import_module(CONTRACTS[framework].module)
    if framework == "lightrag":
        if not all(callable(getattr(components["tokenizer"], method, None)) for method in ("encode", "decode", "split_by_token_limit")):
            raise ComparisonError("LightRAG tokenizer requires the native lossless splitting interface")
        return module.LightRAG(working_dir=str(root), llm_model_func=components["completion"],
                              embedding_func=components["embedding"], tokenizer=components["tokenizer"],
                              llm_model_name=extraction_model, llm_model_max_async=1,
                              embedding_func_max_async=1, max_parallel_insert=1,
                              enable_llm_cache=False, enable_llm_cache_for_entity_extract=False,
                              llm_model_kwargs={"max_tokens": 4096}, vlm_process_enable=False)
    if framework == "hipporag2":
        return module.HippoRAG(global_config=components["config"], save_dir=str(root),
                              extraction_llm=components["extraction_llm"], qa_llm=components["qa_llm"],
                              embedding_model=components["embedding_model"], index_identity=execution_sha,
                              llm_model_name=extraction_model,
                              embedding_model_name="sentence-transformers/all-MiniLM-L6-v2")
    if framework == "graphiti":
        from graphiti_core.embedder.client import EMBEDDING_DIM
        if EMBEDDING_DIM != 384:
            raise ComparisonError("launch the isolated Graphiti process with EMBEDDING_DIM=384 before SDK import")
        return module.Graphiti(llm_client=components["llm_client"], embedder=components["embedder"],
                               cross_encoder=components["cross_encoder"], graph_driver=components["graph_driver"],
                               max_coroutines=1, store_raw_episode_content=True)
    return module.PropertyGraphIndex(nodes=[], llm=components["llm"], embed_model=components["embed_model"],
                                     kg_extractors=components["kg_extractors"],
                                     property_graph_store=components["property_graph_store"],
                                     transformations=components["transformations"], use_async=False)


def _sync(awaitable):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, awaitable).result()


HIPPORAG_TRIPLE_PARSER_VERSION = "hipporag-bare-triples-v2"


def normalize_hipporag_triple_response(content: str, *, finish_reason: str | None,
                                       refusal: Any = None) -> tuple[str, dict[str, Any] | None]:
    """Adapt complete, unambiguous bare triples without changing saved replies.

    Existing object responses with no blank components and unrelated/ambiguous
    formats are returned byte-for-byte. Only the native return value is normalized; the transport's
    request identity, raw response cache and usage remain untouched.
    """
    if refusal is not None:
        raise ComparisonError("HippoRAG refusal is not admitted as extraction evidence")
    if finish_reason != "stop":
        return content, None

    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("ambiguous duplicate JSON field")
            value[key] = item
        return value

    text = content.strip()
    for fence in ("```json\n", "```\n"):
        if text.startswith(fence) and text.endswith("```"):
            text = text[len(fence):-3].strip()
            break
    try:
        payload = json.loads(text, object_pairs_hook=unique_object)
    except (TypeError, ValueError):
        return content, None
    object_envelope = isinstance(payload, dict) and isinstance(payload.get("triples"), list)
    entries = payload["triples"] if object_envelope else payload
    if not isinstance(entries, list) or not entries:
        return content, None
    triples, discarded, reasons = [], [], {}
    if object_envelope:
        entry_format = "object_triples"
        triples = entries
    elif all(isinstance(entry, dict) and set(entry) == {"subject", "predicate", "object"}
             and all(isinstance(value, str) for value in entry.values()) for entry in entries):
        entry_format = "subject_predicate_object"
        triples = [[entry["subject"], entry["predicate"], entry["object"]] for entry in entries]
    elif all(isinstance(entry, list) and 1 <= len(entry) <= 3
             and all(isinstance(value, str) for value in entry) for entry in entries):
        entry_format = "triple_arrays"
        triples = entries
    else:
        return content, None
    accepted = []
    for index, entry in enumerate(triples):
        if not object_envelope and len(entry) != 3:
            discarded.append(index)
            reasons[str(index)] = "incomplete_triple"
        elif (isinstance(entry, list) and len(entry) == 3
              and any(isinstance(value, str) and not any(character.isalnum() for character in value.casefold())
                      for value in entry)):
            # The pinned native text_processing removes non-alphanumeric text.
            # An empty component has no entity/relation value; never invent one.
            discarded.append(index)
            reasons[str(index)] = "blank_component"
        else:
            accepted.append(entry)
    if object_envelope and not discarded:
        return content, None
    if not accepted:
        if all(reason == "incomplete_triple" for reason in reasons.values()):
            return content, None
        raise ComparisonError("HippoRAG response has no complete nonblank triples")
    normalized_payload = {**payload, "triples": accepted} if object_envelope else {"triples": accepted}
    normalized = json.dumps(normalized_payload, ensure_ascii=False, separators=(",", ":"))
    return normalized, {"parser_version": HIPPORAG_TRIPLE_PARSER_VERSION,
                        "entry_format": entry_format, "input_entries": len(entries),
                        "accepted_entries": len(accepted), "discarded_entry_indices": discarded,
                        "discarded_entry_reasons": reasons,
                        "raw_content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                        "normalized_content_sha256": hashlib.sha256(normalized.encode()).hexdigest()}



class MeteredSDKChat:
    """Single-attempt SDK callback; every call reaches the supplied meter.

    Identical requests in one execution context retain the same identity, so a
    native retry cannot turn an unknown dispatch into a new billable request.
    This class has no credentials, HTTP implementation, or provider fallback.
    """
    def __init__(self, transport, *, framework: str, stage: str, execution_id: str,
                 max_output_tokens: int = 4096):
        from .framework_transport import MeteredChatTransport
        if framework not in FRAMEWORKS or stage not in FRAMEWORKS[framework].stages:
            raise ComparisonError("unknown framework callback stage")
        if not isinstance(transport, MeteredChatTransport) or not execution_id or type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 4096:
            raise ComparisonError("explicit metered callback context and output cap required")
        self.transport, self.framework, self.stage = transport, framework, stage
        self.execution_id, self.max_output_tokens = execution_id, max_output_tokens

    async def chat_result(self, messages, **kwargs):
        metadata_only = {"hashing_kv", "timeout"}
        generation = {"temperature", "top_p", "seed", "tools", "tool_choice", "parallel_tool_calls"}
        allowed = metadata_only | generation | {"model", "stream", "n", "response_format", "keyword_extraction",
                   "max_new_tokens", "max_completion_tokens", "max_tokens", "max_output_tokens"}
        if set(kwargs) - allowed:
            raise ComparisonError("unsupported native generation arguments: " + ", ".join(sorted(set(kwargs) - allowed)))
        if kwargs.get("model") not in (None, "gpt-6-luna") or kwargs.get("stream", False) is not False or kwargs.get("n", 1) != 1:
            raise ComparisonError("native model override, streaming or multiple completions is not permitted")
        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]
        else:
            messages = [dict(message) if isinstance(message, Mapping) else
                        {"role": message.role, "content": message.content} for message in messages]
        token_keys = [key for key in ("max_new_tokens", "max_completion_tokens", "max_tokens", "max_output_tokens") if kwargs.get(key) is not None]
        if len(token_keys) > 1:
            raise ComparisonError("ambiguous native output token limit")
        requested = kwargs[token_keys[0]] if token_keys else self.max_output_tokens
        if type(requested) is not int or not 1 <= requested <= self.max_output_tokens:
            raise ComparisonError("native requested output exceeds the fixed callback cap")
        payload = {"model": "gpt-6-luna", "messages": messages,
                   "max_completion_tokens": requested, "stream": False, "n": 1,
                   "reasoning_effort": "none"}
        payload.update({key: kwargs[key] for key in generation if kwargs.get(key) is not None})
        if kwargs.get("response_format") is not None:
            schema = kwargs["response_format"]
            if isinstance(schema, type) and hasattr(schema, "model_json_schema"):
                schema = {"type": "json_schema", "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema()}}
            payload["response_format"] = schema
        elif kwargs.get("keyword_extraction") is True:
            payload["response_format"] = {"type": "json_object"}
        request_id = hashlib.sha256(json.dumps({"execution": self.execution_id, "stage": self.stage, "payload": payload},
                                               sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        result = await self.transport(framework=self.framework, stage=self.stage, request_id=request_id, payload=payload)
        return result

    async def chat(self, messages, **kwargs):
        return (await self.chat_result(messages, **kwargs))["response"]

    async def text(self, prompt, system_prompt=None, history_messages=None, **kwargs):
        messages = ([{"role": "system", "content": system_prompt}] if system_prompt else [])
        messages.extend(history_messages or [])
        messages.append({"role": "user", "content": prompt})
        response = await self.chat(messages, **kwargs)
        return response["choices"][0]["message"]["content"]


GRAPHITI_OUTPUT_CAP_POLICY_VERSION = "graphiti-output-cap-v1"


def make_native_chat(framework: str, hook: MeteredSDKChat, *, config=None):
    """Adapt the pinned SDK's model protocol without constructing its defaults."""
    if hook.framework != framework:
        raise ComparisonError("native callback belongs to a different framework")
    if framework == "lightrag":
        return hook.text
    if framework == "graphiti":
        from graphiti_core.llm_client.client import LLMClient
        from graphiti_core.llm_client.config import LLMConfig
        approved_cap = hook.max_output_tokens
        if type(approved_cap) is not int or not 1 <= approved_cap <= 4096:
            raise ComparisonError("Graphiti requires an explicit approved output cap at most 4096")
        cap_audits = []
        class ControlledLLM(LLMClient):
            async def _generate_response(self, messages, response_model=None, max_tokens=4096, model_size=None, *, model=None):
                if model not in (None, "gpt-6-luna"):
                    raise ComparisonError("native model override is not permitted")
                if type(max_tokens) is not int or max_tokens < 1:
                    raise ComparisonError("Graphiti requested output limit must be a positive integer")
                effective = min(max_tokens, approved_cap)
                cap_audits.append({"policy_version": GRAPHITI_OUTPUT_CAP_POLICY_VERSION,
                                   "native_requested_tokens": max_tokens, "effective_max_tokens": effective,
                                   "approved_max_tokens": approved_cap})
                response = await hook.chat(messages, max_tokens=effective, response_format=response_model)
                content = response["choices"][0]["message"]["content"]
                value = json.loads(content)
                if response_model is not None:
                    return response_model.model_validate(value).model_dump()
                return value
            async def _generate_response_with_retry(self, *args, **kwargs):
                return await self._generate_response(*args, **kwargs)
        result = ControlledLLM(LLMConfig(model="gpt-6-luna", small_model="gpt-6-luna", max_tokens=4096), cache=False)
        result._comparison_hook = hook
        result._comparison_output_cap_policy = hook._comparison_output_cap_policy = GRAPHITI_OUTPUT_CAP_POLICY_VERSION
        result._comparison_output_caps = hook._comparison_output_caps = cap_audits
        return result
    if framework == "hipporag2":
        from hipporag.llm.base import BaseLLM, LLMConfig
        class ControlledLLM(BaseLLM):
            def _init_llm_config(self):
                self.llm_config = LLMConfig.from_dict({"model": "gpt-6-luna"})
            def _native_result(self, result):
                response = result["response"]
                choice = response["choices"][0]
                message = choice["message"]
                if message.get("refusal") is not None:
                    raise ComparisonError("HippoRAG refusal is not admitted as extraction evidence")
                content = message["content"]
                metadata = {**response.get("usage", {}), "finish_reason": choice.get("finish_reason")}
                if hook.stage == "triple_extraction":
                    content, audit = normalize_hipporag_triple_response(
                        content, finish_reason=choice.get("finish_reason"), refusal=message.get("refusal"))
                    if audit is not None:
                        metadata["comparison_response_normalization"] = audit
                        self._comparison_normalizations.append(audit)
                cached = result.get("metering", {}).get("status") == "cached"
                return content, metadata, cached
            def infer(self, messages=None, chat=None, **kwargs):
                return self._native_result(_sync(hook.chat_result(messages if messages is not None else chat, **kwargs)))
            async def ainfer(self, chat=None, **kwargs):
                return self._native_result(await hook.chat_result(chat, **kwargs))
        if config is None or config.llm_name != "gpt-6-luna":
            raise ComparisonError("explicit pinned HippoRAG configuration required")
        client = ControlledLLM(config)
        client._init_llm_config()
        client._comparison_hook = hook
        client._comparison_parser_version = HIPPORAG_TRIPLE_PARSER_VERSION
        client._comparison_normalizations = []
        return client
    if framework == "llamaindex":
        from llama_index.core.llms import CustomLLM, CompletionResponse, LLMMetadata
        class ControlledLLM(CustomLLM):
            @property
            def metadata(self):
                return LLMMetadata(context_window=32768, num_output=4096, model_name="gpt-6-luna")
            def complete(self, prompt, formatted=False, **kwargs):
                return CompletionResponse(text=_sync(hook.text(prompt, **kwargs)))
            async def acomplete(self, prompt, formatted=False, **kwargs):
                return CompletionResponse(text=await hook.text(prompt, **kwargs))
            def stream_complete(self, *args, **kwargs):
                raise ComparisonError("streaming native completions are disabled")
        result = ControlledLLM()
        object.__setattr__(result, "_comparison_hook", hook)
        return result
    if framework == "graphrag":
        from graphrag_llm.completion.completion import LLMCompletion
        from graphrag_llm.types import LLMCompletionResponse
        class ControlledLLM(LLMCompletion):
            def __init__(self, *, tokenizer, metrics_store, **kwargs):
                self._tokenizer, self._metrics_store = tokenizer, metrics_store
            def completion(self, **kwargs):
                return _sync(self.completion_async(**kwargs))
            async def completion_async(self, **kwargs):
                messages = kwargs.pop("messages")
                # The pinned GraphRAG builder adds refusal=None to text-only
                # assistant history. It is response metadata, not evidence.
                # Remove only that exact no-refusal sentinel; a real refusal
                # must stop the controlled text-only route, never disappear.
                normalized = []
                for message in messages:
                    if isinstance(message, Mapping) and message.get("role") == "assistant" and "refusal" in message:
                        if message["refusal"] is not None:
                            raise ComparisonError("GraphRAG assistant refusal is not admitted as text evidence")
                        message = {key: value for key, value in message.items() if key != "refusal"}
                    normalized.append(message)
                schema = kwargs.get("response_format")
                response = LLMCompletionResponse.model_validate(await hook.chat(normalized, **kwargs))
                if isinstance(schema, type) and hasattr(schema, "model_validate_json"):
                    response.formatted_response = schema.model_validate_json(response.content)
                return response
            @property
            def metrics_store(self):
                return self._metrics_store
            @property
            def tokenizer(self):
                return self._tokenizer
        ControlledLLM._comparison_hook = hook
        return ControlledLLM
    raise ComparisonError("unsupported native completion protocol")
