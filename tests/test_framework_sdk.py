import hashlib
from types import SimpleNamespace

import pytest

from auto_zettelkasten.framework_comparison import ComparisonError, FRAMEWORKS
from auto_zettelkasten.framework_sdk import bind_native_call, construct_injected_client, verify_installed_files, verify_sdk_contract


def representation():
    return {"encoder": {"model": "synthetic-local", "runtime": "fixture"}, "split_policy": "fixture-lossless",
            "pool_policy": "fixture-mean-l2", "dimension": 384, "max_tokens": 256, "query_instruction_policy": "reject"}


def test_native_file_pin_is_content_checked_and_cannot_escape(tmp_path):
    (tmp_path / "native.py").write_text("synthetic = True\n")
    digest = hashlib.sha256((tmp_path / "native.py").read_bytes()).hexdigest()
    assert verify_installed_files(tmp_path, {"native.py": digest}) == {"native.py": digest}
    with pytest.raises(ComparisonError, match="differs"):
        verify_installed_files(tmp_path, {"native.py": "0" * 64})
    with pytest.raises(ComparisonError, match="escapes"):
        verify_installed_files(tmp_path, {"../native.py": digest})


def test_native_signature_binding_does_not_execute():
    def native(*, query, context):
        raise AssertionError("must never execute")
    assert bind_native_call(native, query="a", context=[]) == {"query": "a", "context": []}
    with pytest.raises(TypeError):
        bind_native_call(native, query="a")


def test_sdk_rejects_wrong_release_before_optional_import(monkeypatch):
    monkeypatch.setattr("auto_zettelkasten.framework_sdk.importlib.metadata.version", lambda _: "other")
    with pytest.raises(ComparisonError, match="version"):
        verify_sdk_contract("graphiti", expected_commit=FRAMEWORKS["graphiti"].commit)
    with pytest.raises(ComparisonError, match="frozen"):
        verify_sdk_contract("graphiti", expected_commit="wrong")


def test_client_requires_all_inference_components_and_private_storage(tmp_path):
    with pytest.raises(ComparisonError, match="every native"):
        construct_injected_client("graphiti", components={}, working_directory=tmp_path, corpus_identity="a" * 64)
    (tmp_path / ".git").mkdir()
    with pytest.raises(ComparisonError, match="outside Git"):
        construct_injected_client("graphiti", components={}, working_directory=tmp_path / "state", corpus_identity="a" * 64)


def test_lightrag_factory_sets_explicit_components_and_limits(tmp_path, monkeypatch):
    captured = {}
    def factory(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(**kwargs)
    from auto_zettelkasten.framework_embeddings import DerivedEmbeddingTransport
    from auto_zettelkasten.framework_transport import LocalEmbeddingTransport, MeteredChatTransport, MeteredCallLedger
    from auto_zettelkasten.framework_sdk import MeteredSDKChat
    local = LocalEmbeddingTransport(identity={"fixture": "local"}, expected_identity={"fixture": "local"},
        encode=lambda texts: [[1.] + [0.] * 383 for text in texts], token_count=len, resource_guard=lambda: True)
    derived = DerivedEmbeddingTransport(local)
    identity = derived.identity
    completion = MeteredSDKChat(MeteredChatTransport(MeteredCallLedger(tmp_path / "disabled.sqlite3")),
                                framework="lightrag", stage="extraction", execution_id="fixture")
    components = {"completion": completion.text, "embedding": SimpleNamespace(_comparison_embedding_hook=derived,
                  comparison_representation_sha256=derived.representation_sha256)}
    components["tokenizer"] = SimpleNamespace(encode=lambda s: [], decode=lambda t: "", split_by_token_limit=lambda *a, **k: [])
    monkeypatch.setattr("auto_zettelkasten.framework_sdk.verify_sdk_contract", lambda *a, **kw: {})
    monkeypatch.setattr("auto_zettelkasten.framework_sdk.importlib.import_module", lambda _: SimpleNamespace(LightRAG=factory))
    construct_injected_client("lightrag", components=components, working_directory=tmp_path, corpus_identity="a" * 64,
                              embedding_identity=identity)
    assert captured["llm_model_func"] is components["completion"]
    assert captured["embedding_func"] is components["embedding"]
    assert captured["max_parallel_insert"] == captured["llm_model_max_async"] == 1
    assert captured["enable_llm_cache"] is False
    assert captured["vlm_process_enable"] is False
    assert len(__import__("pathlib").Path(captured["working_dir"]).name) == 64
    __import__("pathlib").Path(captured["working_dir"]).mkdir()
    with pytest.raises(ComparisonError, match="fresh native state"):
        construct_injected_client("lightrag", components=components, working_directory=tmp_path,
                                  corpus_identity="a" * 64, embedding_identity=identity)
    with pytest.raises(ComparisonError, match="metered SDK"):
        construct_injected_client("lightrag", components={**components, "completion": lambda: None},
                                  working_directory=tmp_path / "different", corpus_identity="a" * 64, embedding_identity=identity)


def test_execution_identity_separates_corpus_model_and_pooling():
    from auto_zettelkasten.framework_sdk import comparison_execution_identity
    identity = representation()
    a = comparison_execution_identity("lightrag", corpus_identity="a" * 64, embedding_identity=identity)
    assert a != comparison_execution_identity("lightrag", corpus_identity="b" * 64, embedding_identity=identity)
    assert a != comparison_execution_identity("lightrag", corpus_identity="a" * 64, embedding_identity={**identity, "pool_policy": "different"})
    assert a != comparison_execution_identity("graphiti", corpus_identity="a" * 64, embedding_identity=identity)


def metered_fixture(tmp_path, framework, stage, calls):
    from auto_zettelkasten.framework_comparison import StageBound
    from auto_zettelkasten.framework_transport import MeteredChatTransport, MeteredCallLedger, TransportApproval
    bound = StageBound(framework, stage, 8, 32768, 4096, "0.0000001", "0.0000005", "https://example.org/prices", "gpt-6-luna")
    approval = TransportApproval("synthetic-only", "a" * 64, "b" * 64,
        "https://example.org/chat/completions", "0.1", ((framework, "0.1"),), (bound,))
    async def backend(**kwargs):
        calls.append(kwargs)
        return {"model": "gpt-6-luna", "service_tier": "default",
                "choices": [{"message": {"role": "assistant", "content": "synthetic"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 5, "prompt_tokens_details": {"cache_write_tokens": 0}}}
    return MeteredChatTransport(MeteredCallLedger(tmp_path / "meter.sqlite3", approval), backend=backend, cache_responses=True)


def test_callback_normalizes_tokens_and_reuses_identity_without_fallback(tmp_path):
    import asyncio
    from auto_zettelkasten.framework_sdk import MeteredSDKChat
    calls = []
    meter = metered_fixture(tmp_path, "lightrag", "extraction", calls)
    hook = MeteredSDKChat(meter, framework="lightrag", stage="extraction", execution_id="synthetic")
    assert asyncio.run(hook.text("same", max_new_tokens=32)) == "synthetic"
    assert asyncio.run(hook.text("same", max_tokens=32)) == "synthetic"
    assert len(calls) == 1
    assert len(meter.ledger.snapshot()["calls"]) == 1
    assert calls[0]["payload"]["max_completion_tokens"] == 32
    assert calls[0]["payload"]["reasoning_effort"] == "none"
    with pytest.raises(ComparisonError, match="ambiguous"):
        asyncio.run(hook.text("x", max_tokens=1, max_new_tokens=1))
    with pytest.raises(ComparisonError, match="exceeds"):
        asyncio.run(hook.text("x", max_tokens=5000))
    with pytest.raises(ComparisonError, match="model override"):
        asyncio.run(hook.text("x", model="another"))
    with pytest.raises(ComparisonError, match="unsupported native"):
        asyncio.run(hook.text("x", thinking={"budget": 1000}))
    asyncio.run(hook.text("x", temperature=0, keyword_extraction=True, hashing_kv=object()))
    assert calls[-1]["payload"]["temperature"] == 0
    assert calls[-1]["payload"]["response_format"] == {"type": "json_object"}


def test_callback_preserves_per_call_metering_for_cache_reporting(tmp_path):
    import asyncio
    from auto_zettelkasten.framework_sdk import MeteredSDKChat
    meter = metered_fixture(tmp_path, "hipporag2", "query", [])
    hook = MeteredSDKChat(meter, framework="hipporag2", stage="query", execution_id="synthetic")
    asyncio.run(hook.chat_result("a"))
    result = asyncio.run(hook.chat_result("a"))
    assert result["metering"]["status"] == "cached"
    assert result["metering"]["provider_calls"] == 0
    assert asyncio.run(hook.chat("a"))["choices"][0]["message"]["content"] == "synthetic"


def graphrag_client_fixture(tmp_path, monkeypatch, calls):
    """Use the real metered boundary with an optional-SDK protocol stand-in."""
    import sys
    from auto_zettelkasten.framework_sdk import MeteredSDKChat, make_native_chat
    monkeypatch.setitem(sys.modules, "graphrag_llm.completion.completion",
                        SimpleNamespace(LLMCompletion=object))
    monkeypatch.setitem(sys.modules, "graphrag_llm.types",
                        SimpleNamespace(LLMCompletionResponse=SimpleNamespace(model_validate=lambda value: value)))
    meter = metered_fixture(tmp_path, "graphrag", "extraction", calls)
    hook = MeteredSDKChat(meter, framework="graphrag", stage="extraction", execution_id="synthetic")
    return make_native_chat("graphrag", hook)(tokenizer=None, metrics_store=None), meter


def test_graphrag_null_refusal_history_normalizes_without_changing_cache_identity(tmp_path, monkeypatch):
    import asyncio
    calls = []
    client, meter = graphrag_client_fixture(tmp_path, monkeypatch, calls)
    history = [{"role": "user", "content": "synthetic extraction"},
               {"role": "assistant", "content": "synthetic previous evidence", "refusal": None, "name": "extractor"},
               {"role": "user", "content": "continue"}]
    asyncio.run(client.completion_async(messages=history))
    expected = [history[0], {"role": "assistant", "content": "synthetic previous evidence", "name": "extractor"}, history[2]]
    assert calls[0]["payload"]["messages"] == expected
    assert history[1]["refusal"] is None  # Never mutate GraphRAG's conversation.
    asyncio.run(client.completion_async(messages=expected))
    assert len(calls) == len(meter.ledger.snapshot()["calls"]) == 1


@pytest.mark.parametrize("extra", [
    {"refusal": "cannot provide this"}, {"refusal": ""},
    {"refusal": None, "audio": None}, {"refusal": None, "function_call": {"name": "x"}},
    {"refusal": None, "unexpected": None},
])
def test_graphrag_history_rejects_refusals_and_unknown_fields_before_reservation(tmp_path, monkeypatch, extra):
    import asyncio
    calls = []
    client, meter = graphrag_client_fixture(tmp_path, monkeypatch, calls)
    with pytest.raises(ComparisonError):
        asyncio.run(client.completion_async(messages=[{"role": "assistant", "content": "synthetic", **extra}]))
    assert calls == []
    assert meter.ledger.snapshot()["calls"] == []


def test_graphrag_history_preserves_tool_fields_and_other_roles_are_still_strict(tmp_path, monkeypatch):
    import asyncio
    calls = []
    client, meter = graphrag_client_fixture(tmp_path, monkeypatch, calls)
    message = {"role": "assistant", "content": "synthetic", "refusal": None,
               "tool_calls": [{"id": "synthetic", "type": "function", "function": {"name": "f", "arguments": "{}"}}]}
    asyncio.run(client.completion_async(messages=[message]))
    assert calls[0]["payload"]["messages"][0]["tool_calls"] == message["tool_calls"]
    with pytest.raises(ComparisonError, match="unapproved message fields"):
        asyncio.run(client.completion_async(messages=[{"role": "user", "content": "synthetic", "refusal": None}]))
    assert len(meter.ledger.snapshot()["calls"]) == 1


def test_hippo_bare_triples_normalize_only_known_shapes_with_audited_drops():
    import json
    from auto_zettelkasten.framework_sdk import normalize_hipporag_triple_response
    raw = json.dumps([["A", "relates", "B"], ["incomplete", "entry"], ["incomplete"], ["C", "uses", "D"]])
    content, audit = normalize_hipporag_triple_response(raw, finish_reason="stop")
    assert json.loads(content) == {"triples": [["A", "relates", "B"], ["C", "uses", "D"]]}
    assert audit["discarded_entry_indices"] == [1, 2]
    assert audit["input_entries"] == 4 and audit["accepted_entries"] == 2
    assert audit["raw_content_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert audit["normalized_content_sha256"] == hashlib.sha256(content.encode()).hexdigest()
    assert audit["parser_version"] == "hipporag-bare-triples-v2"
    objects = '[{"object":"B","subject":"A","predicate":"relates"}]'
    content, audit = normalize_hipporag_triple_response(objects, finish_reason="stop")
    assert json.loads(content) == {"triples": [["A", "relates", "B"]]}
    assert audit["entry_format"] == "subject_predicate_object" and audit["discarded_entry_indices"] == []


@pytest.mark.parametrize("raw", [
    '{ "triples": [["A","relates","B"]] }', '["A", "B"]', '[]',
    '[["A", "relates", "B"], {"subject":"A","predicate":"relates","object":"B"}]',
    '[{"subject":"A","predicate":"relates","object":"B","extra":"unknown"}]',
    '[{"subject":"A","subject":"changed","predicate":"relates","object":"B"}]',
    '[["A", "relates", 2]]', '[["A", "relates"]]',
    '[["A", "relates", "B", "extra"]]', 'text [["A", "relates", "B"]]',
])
def test_hippo_normalizer_preserves_existing_or_ambiguous_responses(raw):
    from auto_zettelkasten.framework_sdk import normalize_hipporag_triple_response
    assert normalize_hipporag_triple_response(raw, finish_reason="stop") == (raw, None)


def test_hippo_normalizer_never_repairs_truncation_or_refusals():
    from auto_zettelkasten.framework_sdk import normalize_hipporag_triple_response
    raw = '[["A", "relates", "B"]]'
    for reason in ("length", "content_filter", None):
        assert normalize_hipporag_triple_response(raw, finish_reason=reason) == (raw, None)
    for refusal in ("declined", ""):
        with pytest.raises(ComparisonError, match="refusal"):
            normalize_hipporag_triple_response(raw, finish_reason="stop", refusal=refusal)


def hippo_client_fixture(tmp_path, monkeypatch, raw, stage="triple_extraction"):
    import sys
    from auto_zettelkasten.framework_sdk import MeteredSDKChat, make_native_chat
    class BaseLLM:
        def __init__(self, config):
            self.global_config = config
    monkeypatch.setitem(sys.modules, "hipporag.llm.base", SimpleNamespace(
        BaseLLM=BaseLLM, LLMConfig=SimpleNamespace(from_dict=lambda value: value)))
    meter = metered_fixture(tmp_path, "hipporag2", stage, [])
    calls, responses = [], []
    async def backend(**kwargs):
        calls.append(kwargs)
        response = {"model": "gpt-6-luna", "service_tier": "default", "choices": [
            {"finish_reason": "stop", "message": {"role": "assistant", "content": raw}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 5, "prompt_tokens_details": {"cache_write_tokens": 0}}}
        responses.append(response)
        return response
    meter.backend = backend
    hook = MeteredSDKChat(meter, framework="hipporag2", stage=stage, execution_id="synthetic")
    return make_native_chat("hipporag2", hook, config=SimpleNamespace(llm_name="gpt-6-luna")), meter, calls, responses


def test_hippo_infer_and_ainfer_normalize_after_raw_response_caching(tmp_path, monkeypatch):
    import asyncio
    import json
    raw = '[["A","relates","B"],["incomplete"]]'
    client, meter, calls, responses = hippo_client_fixture(tmp_path, monkeypatch, raw)
    content, metadata, cached = client.infer(messages=[{"role": "user", "content": "synthetic"}])
    assert json.loads(content) == {"triples": [["A", "relates", "B"]]}
    assert not cached
    assert responses[0]["choices"][0]["message"]["content"] == raw
    assert metadata["comparison_response_normalization"]["discarded_entry_indices"] == [1]
    again = asyncio.run(client.ainfer(chat=[{"role": "user", "content": "synthetic"}]))
    assert again[:2] == (content, metadata) and again[2]
    assert len(calls) == len(meter.ledger.snapshot()["calls"]) == 1
    assert client._comparison_parser_version == "hipporag-bare-triples-v2"
    assert len(client._comparison_normalizations) == 2
    import sqlite3
    with sqlite3.connect(meter.ledger.path) as db:
        saved = json.loads(db.execute("SELECT response_json FROM transport_calls").fetchone()[0])
    assert saved["response"]["choices"][0]["message"]["content"] == raw


def test_hippo_query_hook_does_not_reinterpret_bare_triple_arrays(tmp_path, monkeypatch):
    raw = '[["A","relates","B"]]'
    client, meter, calls, responses = hippo_client_fixture(tmp_path, monkeypatch, raw, stage="query_filter")
    content, metadata, cached = client.infer(messages=[{"role": "user", "content": "synthetic"}])
    assert content == raw
    assert "comparison_response_normalization" not in metadata
    assert client._comparison_normalizations == []


@pytest.mark.parametrize("wrapper", ["{}", "```json\n{}\n```", "```\n{}\n```"])
def test_hippo_v2_drops_blank_components_in_object_without_inventing_values(wrapper):
    import json
    from auto_zettelkasten.framework_sdk import normalize_hipporag_triple_response
    good = ["A", "relates", "B"]
    value = {"triples": [good, ["A", "relates", ""], ["A", "   ", "B"], ["---", "relates", "B"]], "metadata": "preserved"}
    raw = wrapper.format(json.dumps(value))
    content, audit = normalize_hipporag_triple_response(raw, finish_reason="stop")
    assert json.loads(content) == {"triples": [good], "metadata": "preserved"}
    assert audit["discarded_entry_indices"] == [1, 2, 3]
    assert audit["discarded_entry_reasons"] == {"1": "blank_component", "2": "blank_component", "3": "blank_component"}
    assert audit["parser_version"] == "hipporag-bare-triples-v2"
    valid = wrapper.format('{ "triples": [["A", "relates", "B"]] }')
    assert normalize_hipporag_triple_response(valid, finish_reason="stop") == (valid, None)


def test_hippo_v2_blank_filter_covers_both_supported_bare_shapes():
    import json
    from auto_zettelkasten.framework_sdk import normalize_hipporag_triple_response
    for raw in ('[["A","relates","B"],["A","relates",""]]',
                '[{"subject":"A","predicate":"relates","object":"B"},{"subject":"A","predicate":"relates","object":""}]'):
        content, audit = normalize_hipporag_triple_response(raw, finish_reason="stop")
        assert json.loads(content) == {"triples": [["A", "relates", "B"]]}
        assert audit["discarded_entry_indices"] == [1]
        assert audit["discarded_entry_reasons"] == {"1": "blank_component"}


@pytest.mark.parametrize("raw", ['{"triples":[["A","relates",""]]}', '[["A","relates",""]]'])
def test_hippo_v2_all_blank_candidates_stop_instead_of_creating_empty_success(raw):
    from auto_zettelkasten.framework_sdk import normalize_hipporag_triple_response
    with pytest.raises(ComparisonError, match="no complete nonblank triples"):
        normalize_hipporag_triple_response(raw, finish_reason="stop")


def test_hippo_v2_blank_detection_matches_native_casefold_before_alnum():
    from auto_zettelkasten.framework_sdk import normalize_hipporag_triple_response
    # Combining Greek ypogegrammeni casefolds to an alphabetic iota.
    raw = '{"triples":[["\u0345","relates","B"]]}'
    assert normalize_hipporag_triple_response(raw, finish_reason="stop") == (raw, None)


def graphiti_client_fixture(tmp_path, monkeypatch, *, output_cap=4096):
    import sys
    from auto_zettelkasten.framework_sdk import MeteredSDKChat, make_native_chat
    class LLMClient:
        def __init__(self, config, *, cache):
            self.config = config
    monkeypatch.setitem(sys.modules, "graphiti_core.llm_client.client", SimpleNamespace(LLMClient=LLMClient))
    monkeypatch.setitem(sys.modules, "graphiti_core.llm_client.config", SimpleNamespace(LLMConfig=lambda **kwargs: SimpleNamespace(**kwargs)))
    meter = metered_fixture(tmp_path, "graphiti", "fact_extraction", [])
    calls = []
    async def backend(**kwargs):
        calls.append(kwargs)
        return {"model": "gpt-6-luna", "service_tier": "default", "choices": [
            {"finish_reason": "stop", "message": {"role": "assistant", "content": '{"edges":[]}'}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 5, "prompt_tokens_details": {"cache_write_tokens": 0}}}
    meter.backend = backend
    hook = MeteredSDKChat(meter, framework="graphiti", stage="fact_extraction", execution_id="synthetic", max_output_tokens=output_cap)
    return make_native_chat("graphiti", hook), hook, calls


@pytest.mark.parametrize("requested,approved,effective", [(16384, 4096, 4096), (512, 4096, 512), (16384, 1024, 1024)])
def test_graphiti_adapter_caps_only_native_requested_output_with_explicit_audit(tmp_path, monkeypatch, requested, approved, effective):
    import asyncio
    client, hook, calls = graphiti_client_fixture(tmp_path, monkeypatch, output_cap=approved)
    messages = [{"role": "user", "content": "synthetic unchanged evidence"}]
    result = asyncio.run(client._generate_response(messages, max_tokens=requested))
    assert result == {"edges": []}
    assert calls[0]["payload"]["messages"] == messages
    assert calls[0]["payload"]["max_completion_tokens"] == effective
    assert hook.max_output_tokens == approved
    assert client._comparison_output_cap_policy == "graphiti-output-cap-v1"
    assert client._comparison_output_caps == [{"policy_version": "graphiti-output-cap-v1", "native_requested_tokens": requested,
                                               "effective_max_tokens": effective, "approved_max_tokens": approved}]
    assert hook._comparison_output_caps is client._comparison_output_caps
    with pytest.raises(ComparisonError, match="exceeds"):
        asyncio.run(hook.chat(messages, max_tokens=16384))
    assert len(calls) == 1  # The generic metered boundary still rejects oversize calls.


@pytest.mark.parametrize("invalid", [None, 0, -1, True, 4.5, "4096"])
def test_graphiti_adapter_rejects_invalid_requested_limit_before_dispatch(tmp_path, monkeypatch, invalid):
    import asyncio
    client, hook, calls = graphiti_client_fixture(tmp_path, monkeypatch)
    with pytest.raises(ComparisonError, match="positive integer"):
        asyncio.run(client._generate_response([{"role": "user", "content": "synthetic"}], max_tokens=invalid))
    assert calls == [] and client._comparison_output_caps == []


def test_graphiti_adapter_retains_exact_cache_identity_for_admitted_payload(tmp_path, monkeypatch):
    import asyncio
    client, hook, calls = graphiti_client_fixture(tmp_path, monkeypatch)
    messages = [{"role": "user", "content": "synthetic unchanged evidence"}]
    asyncio.run(hook.chat(messages, max_tokens=4096))
    asyncio.run(client._generate_response(messages, max_tokens=16384))
    assert len(calls) == 1
    assert len(hook.transport.ledger.snapshot()["calls"]) == 1
