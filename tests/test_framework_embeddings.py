import asyncio
import importlib
import json
import math

import pytest

from auto_zettelkasten.framework_comparison import ComparisonError
from auto_zettelkasten.framework_embeddings import (
    DerivedEmbeddingTransport, lossless_token_chunks, make_native_embedding,
)
from auto_zettelkasten.framework_transport import LocalEmbeddingTransport


def fixture_transport(*, guard=None, encode=None, **kwargs):
    seen = []
    def encoder(texts):
        seen.extend(texts)
        return [[1.0 if index == (ord(text[0]) % 2) else 0.0 for index in range(384)] for text in texts]
    identity = {"encoder_sha256": "a" * 64, "tokenizer_sha256": "b" * 64, "synthetic": True}
    transport = LocalEmbeddingTransport(identity=identity, expected_identity=identity,
        encode=encode or encoder, token_count=lambda text: len(text.encode()), resource_guard=guard or (lambda: True), **kwargs)
    return transport, seen


def test_lossless_unicode_split_is_deterministic_and_bounded():
    text = "  Café 中文\n" * 90 + "tail  "
    pieces = lossless_token_chunks(text, lambda value: len(value.encode()))
    assert "".join(pieces) == text
    assert len(pieces) > 1
    assert all(0 < len(piece.encode()) <= 256 and piece.strip() for piece in pieces)
    assert pieces == lossless_token_chunks(text, lambda value: len(value.encode()))


@pytest.mark.parametrize("text", ["", "  ", None, "x" * 2_000_001])
def test_bad_text_refused(text):
    with pytest.raises(ComparisonError):
        lossless_token_chunks(text, len)


def test_invalid_counter_and_unsplittable_codepoint_refused():
    for counter in [lambda text: True, lambda text: -1, lambda text: 0]:
        with pytest.raises(ComparisonError):
            lossless_token_chunks("abc", counter)
    with pytest.raises(ComparisonError):
        lossless_token_chunks("中", lambda text: len(text.encode()), max_tokens=1)


def test_short_vectors_unchanged_and_identity_binds_full_encoder():
    transport, seen = fixture_transport()
    hook = DerivedEmbeddingTransport(transport)
    value = asyncio.run(hook(["short"]))
    assert value == [[0.0, 1.0] + [0.0] * 382]
    assert seen == ["short"]
    assert hook.identity['encoder'] == transport.identity
    assert hook.identity['split_policy'] == 'lossless-codepoint-prefix-bisection-v1'
    assert hook.identity['pool_policy'] == 'equal-mean-of-unit-chunks-then-l2-v1'
    assert len(hook.representation_sha256) == 64
    assert DerivedEmbeddingTransport(fixture_transport()[0]).representation_sha256 == hook.representation_sha256
    transport.identity['tokenizer_sha256'] = 'c' * 64
    assert DerivedEmbeddingTransport(transport).representation_sha256 != hook.representation_sha256
    assert hook.identity['encoder']['tokenizer_sha256'] == 'b' * 64
    with pytest.raises(ComparisonError, match='identity'):
        asyncio.run(hook(['changed encoder']))


def test_long_text_mean_is_unit_normalized_and_exact_chunks_reach_transport():
    transport, seen = fixture_transport()
    hook = DerivedEmbeddingTransport(transport)
    text = 'a' * 256 + 'b' * 256
    vector = asyncio.run(hook([text]))[0]
    assert seen == ['a' * 256, 'b' * 256]
    assert vector[:2] == pytest.approx([math.sqrt(0.5)] * 2)
    assert sum(value * value for value in vector) == pytest.approx(1)


def test_more_than_32_chunks_batched_and_guard_runs_before_and_after_each_batch():
    guards = []
    transport, seen = fixture_transport(guard=lambda: guards.append(True))
    text = 'x' * (256 * 33)
    vector = asyncio.run(DerivedEmbeddingTransport(transport)([text]))[0]
    assert ''.join(seen) == text
    assert transport.calls == 2
    assert len(guards) >= 5
    assert len(vector) == 384


def test_entire_batch_validated_before_any_inference():
    transport, seen = fixture_transport()
    for texts in ['string', [], ["valid", "  "], ["valid", 2]]:
        with pytest.raises(ComparisonError):
            asyncio.run(DerivedEmbeddingTransport(transport)(texts))
    assert not seen and transport.calls == 0


def test_guard_and_zero_mean_fail_closed():
    transport, seen = fixture_transport(guard=lambda: False)
    with pytest.raises(ComparisonError, match='guard'):
        asyncio.run(DerivedEmbeddingTransport(transport)(['text']))
    assert not seen
    def cancel(texts):
        return [[1.0 if text[0] == 'a' else -1.0] + [0.0] * 383 for text in texts]
    transport, _ = fixture_transport(encode=cancel)
    with pytest.raises(ComparisonError, match='direction'):
        asyncio.run(DerivedEmbeddingTransport(transport)(['a' * 256 + 'b' * 256]))


def test_fixed_dimensions_and_unknown_framework():
    with pytest.raises(ComparisonError):
        DerivedEmbeddingTransport(fixture_transport(dimension=1024)[0])
    with pytest.raises(ComparisonError):
        DerivedEmbeddingTransport(fixture_transport(max_tokens=512)[0])
    with pytest.raises(ComparisonError):
        make_native_embedding('unknown', DerivedEmbeddingTransport(fixture_transport()[0]))


def test_pressure_rejects_before_tokenization_and_oversized_aggregate_before_split():
    transport, seen = fixture_transport(guard=lambda: False)
    def no_tokenization(text):
        raise AssertionError('pressure guard must run before tokenization')
    transport.token_count = no_tokenization
    with pytest.raises(ComparisonError, match='guard'):
        asyncio.run(DerivedEmbeddingTransport(transport)(['long text' * 1000]))
    transport, _ = fixture_transport()
    transport.token_count = no_tokenization
    with pytest.raises(ComparisonError, match='aggregate'):
        asyncio.run(DerivedEmbeddingTransport(transport)(['x' * 2_000_000] * 34))
    assert not seen


def test_explicit_instruction_policy_changes_representation_identity():
    transport, _ = fixture_transport()
    rejecting = DerivedEmbeddingTransport(transport)
    ignoring = DerivedEmbeddingTransport(transport, query_instruction_policy='ignore')
    assert rejecting.identity['query_instruction_policy'] == 'reject'
    assert ignoring.identity['query_instruction_policy'] == 'ignore'
    assert rejecting.representation_sha256 != ignoring.representation_sha256
    with pytest.raises(AttributeError):
        rejecting.representation_sha256 = 'changed'


def test_representation_carrier_binds_complete_defensive_identity():
    from auto_zettelkasten.framework_embeddings import _carry_representation
    class Component:
        pass
    hook = DerivedEmbeddingTransport(fixture_transport()[0])
    for component in [Component(), Component]:
        _carry_representation(component, hook)
        assert component._comparison_embedding_hook is hook
        assert component.comparison_representation_sha256 == hook.representation_sha256
        assert json.loads(component.comparison_representation_identity_json) == hook.identity
        changed = hook.identity
        changed['pool_policy'] = 'changed'
        assert json.loads(component.comparison_representation_identity_json) != changed


def test_module_import_does_not_import_optional_sdks(monkeypatch):
    import builtins
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        assert name.split('.')[0] not in {'lightrag', 'graphiti_core', 'hipporag', 'llama_index', 'graphrag_llm', 'numpy'}
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', guarded)
    from auto_zettelkasten import framework_embeddings
    spec = importlib.util.spec_from_file_location('auto_zettelkasten._embedding_import_probe', framework_embeddings.__file__)
    spec.loader.exec_module(importlib.util.module_from_spec(spec))


def test_short_texts_share_one_encoder_batch_without_changing_output_order():
    batches = []
    def encode(texts):
        batches.append(list(texts))
        return [[1.0 if index == ord(text[0]) % 384 else 0.0 for index in range(384)] for text in texts]
    transport, _ = fixture_transport(encode=encode)
    texts = [chr(65 + index) + ' note' for index in range(32)]
    result = asyncio.run(DerivedEmbeddingTransport(transport)(texts))
    assert batches == [texts]
    assert transport.calls == 1
    assert [vector.index(1.0) for vector in result] == [ord(text[0]) for text in texts]


def test_cross_text_batches_preserve_exact_sequential_pooling_and_chunk_order():
    texts = ['a' * 256 + 'b' * 256, 'c' * (256 * 31), 'd', 'e' * 256 + 'f' * 256, 'Café 中文' * 60]
    def vector(text):
        values = [float(sum(text.encode()) % 11 + 1), float(len(text.encode()) % 7 + 1), float(ord(text[0]) % 5 + 1)]
        norm = math.sqrt(math.fsum(value * value for value in values))
        return [value / norm for value in values] + [0.0] * 381
    batches = []
    def encode(batch):
        batches.append(list(batch))
        return [vector(text) for text in batch]
    transport, _ = fixture_transport(encode=encode)
    hook = DerivedEmbeddingTransport(transport)
    before_identity = hook.representation_sha256
    groups = [lossless_token_chunks(text, lambda value: len(value.encode())) for text in texts]
    expected = []
    for pieces in groups:
        vectors = [vector(piece) for piece in pieces]
        if len(vectors) == 1:
            expected.append(vectors[0])
        else:
            mean = [math.fsum(row[index] for row in vectors) / len(vectors) for index in range(384)]
            norm = math.sqrt(math.fsum(value * value for value in mean))
            expected.append([value / norm for value in mean])
    actual = asyncio.run(hook(texts))
    assert actual == expected
    assert [piece for batch in batches for piece in batch] == [piece for group in groups for piece in group]
    assert all(1 <= len(batch) <= 32 for batch in batches)
    assert transport.calls == math.ceil(sum(map(len, groups)) / 32)
    assert hook.representation_sha256 == before_identity


def test_cross_text_batches_stop_before_next_inference_when_guard_changes():
    batches = []
    def guard():
        # Preparation is admitted. After the first full batch has returned,
        # the next transport admission rejects before any second encode call.
        return len(batches) < 1
    def encode(texts):
        batches.append(list(texts))
        return [[1.0] + [0.0] * 383 for _ in texts]
    transport, _ = fixture_transport(guard=guard, encode=encode)
    with pytest.raises(ComparisonError, match='guard'):
        asyncio.run(DerivedEmbeddingTransport(transport)(['x' + str(i) for i in range(33)]))
    assert len(batches) == 1 and len(batches[0]) == 32
    assert transport.calls == 1


def hippo_embedder_fixture(monkeypatch, hook):
    pytest.importorskip("numpy")
    import sys
    from types import SimpleNamespace
    class BaseEmbeddingModel:
        def __init__(self, config):
            self.global_config = config
    monkeypatch.setitem(sys.modules, 'hipporag.embedding_model.base', SimpleNamespace(
        BaseEmbeddingModel=BaseEmbeddingModel,
        EmbeddingConfig=SimpleNamespace(from_dict=lambda value: value)))
    return make_native_embedding('hipporag2', hook, config=SimpleNamespace(
        embedding_model_name='sentence-transformers/all-MiniLM-L6-v2'))


@pytest.mark.parametrize('count', [1024, 1025, 2050])
def test_hippo_large_logical_batches_preserve_vectors_order_and_derived_limits(monkeypatch, count):
    np = pytest.importorskip("numpy")
    guards, batches = [], []
    transport, seen = fixture_transport(guard=lambda: guards.append(True))
    class RecordingDerived(DerivedEmbeddingTransport):
        async def __call__(self, texts):
            batches.append(list(texts))
            return await super().__call__(texts)
    hook = RecordingDerived(transport)
    identity = hook.representation_sha256
    client = hippo_embedder_fixture(monkeypatch, hook)
    texts = [chr(65 + index % 26) + str(index) for index in range(count)]
    # A multi-chunk input at the SDK boundary also retains its original pooling.
    texts[1023] = 'a' * 256 + 'b' * 256
    actual = client.batch_encode(texts)
    expected_hook = DerivedEmbeddingTransport(fixture_transport()[0])
    expected = np.asarray([asyncio.run(expected_hook([text]))[0] for text in texts], dtype=np.float32)
    assert np.array_equal(actual, expected)
    assert actual.shape == (count, 384) and actual.dtype == np.float32
    assert [len(batch) for batch in batches] == [len(texts[start:start + 1024]) for start in range(0, count, 1024)]
    assert [text for batch in batches for text in batch] == texts
    assert seen == [piece for text in texts for piece in lossless_token_chunks(text, lambda value: len(value.encode()))]
    assert len(guards) == count + 3 * transport.calls
    assert hook.representation_sha256 == identity == client.comparison_representation_sha256
    assert client._comparison_batch_policy == 'hipporag-derived-batches-1024-v1'
    with pytest.raises(ComparisonError, match='1–1024'):
        asyncio.run(hook(['x'] * 1025))


def test_hippo_batch_validation_and_single_query_behavior_stay_strict(monkeypatch):
    np = pytest.importorskip("numpy")
    transport, seen = fixture_transport()
    client = hippo_embedder_fixture(monkeypatch, DerivedEmbeddingTransport(transport))
    for texts in ([], b'bytes', None, iter(['x']), ['x'] * 1024 + [' '], ['x'] * 1024 + [2]):
        with pytest.raises(ComparisonError):
            client.batch_encode(texts)
    with pytest.raises(ComparisonError, match='aggregate'):
        client.batch_encode(['x' * 2_000_000] * 34)
    assert not seen and transport.calls == 0
    result = client.batch_encode('query')
    assert result.shape == (1, 384) and result.dtype == np.float32
    with pytest.raises(ComparisonError, match='options'):
        client.batch_encode('query', norm=False)
    with pytest.raises(ComparisonError, match='instruction'):
        client.batch_encode('query', instruction='different representation')


def test_hippo_large_batch_stops_when_next_derived_call_guard_denies(monkeypatch):
    batches, admitted = [], {'value': True}
    transport, seen = fixture_transport(guard=lambda: admitted['value'])
    class StopNextDerived(DerivedEmbeddingTransport):
        async def __call__(self, texts):
            batches.append(len(texts))
            if len(batches) == 2:
                admitted['value'] = False
            return await super().__call__(texts)
    client = hippo_embedder_fixture(monkeypatch, StopNextDerived(transport))
    with pytest.raises(ComparisonError, match='guard'):
        client.batch_encode(['x'] * 1025)
    assert batches == [1024, 1]
    assert len(seen) == 1024 and transport.calls == 32
