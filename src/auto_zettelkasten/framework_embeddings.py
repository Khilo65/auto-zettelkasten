"""Local-only native embedding seams and a versioned derived-text representation.

Optional SDK imports occur only in make_native_embedding. This module never
loads an encoder, resolves credentials, or changes canonical source vectors.
"""
from __future__ import annotations

from collections.abc import Sequence
import hashlib
import inspect
import json
import math

from .framework_comparison import ComparisonError
from .framework_sdk import _sync
from .framework_transport import LocalEmbeddingTransport

MODEL = "sentence-transformers/all-MiniLM-L6-v2"
SPLIT_POLICY = "lossless-codepoint-prefix-bisection-v1"
POOL_POLICY = "equal-mean-of-unit-chunks-then-l2-v1"
HIPPORAG_BATCH_POLICY = "hipporag-derived-batches-1024-v1"


def lossless_token_chunks(text, token_count, *, max_tokens=256):
    """Split exact Unicode prefixes; every emitted piece is independently checked.

    Bisection chooses a valid prefix, without claiming a globally longest prefix
    or assuming token counts are monotonic. No encode/decode round trip is used.
    Inputs that cannot produce substantive bounded pieces fail before inference.
    """
    if not isinstance(text, str) or not text.strip() or len(text) > 2_000_000:
        raise ComparisonError("bounded substantive derived text required")
    if not callable(token_count) or type(max_tokens) is not int or max_tokens < 1:
        raise ComparisonError("explicit token counter and positive window required")
    def count(piece):
        value = token_count(piece)
        if type(value) is not int or value < 0:
            raise ComparisonError("token counter must return a nonnegative integer")
        return value
    pieces, start = [], 0
    while start < len(text):
        rest = text[start:]
        # Bound tokenizer work to expanding prefixes rather than repeatedly
        # tokenizing a potentially megabyte-long remaining suffix.
        high = min(max_tokens, len(rest))
        size = count(rest[:high])
        while size <= max_tokens and high < len(rest):
            high = min(high * 2, len(rest))
            size = count(rest[:high])
        if high == len(rest) and 1 <= size <= max_tokens:
            pieces.append(rest)
            break
        low, high, best = 1, high - 1, 0
        while low <= high:
            middle = (low + high) // 2
            candidate = rest[:middle]
            size = count(candidate)
            if not candidate.strip() or size == 0:
                low = middle + 1
            elif size <= max_tokens:
                best, low = middle, middle + 1
            else:
                high = middle - 1
        if not best:
            raise ComparisonError("derived text cannot be losslessly split into substantive token windows")
        piece = rest[:best]
        if not rest[best:].strip():
            raise ComparisonError("trailing whitespace cannot fit a substantive token window")
        pieces.append(piece)
        start += best
        if len(pieces) >= 8192:
            raise ComparisonError("derived text exceeds bounded chunk count")
    if "".join(pieces) != text or any(not p.strip() or not 1 <= count(p) <= max_tokens for p in pieces):
        raise ComparisonError("lossless derived-text representation verification failed")
    return pieces


class DerivedEmbeddingTransport:
    """Pool long derived text through the caller's already verified local encoder."""
    def __init__(self, transport: LocalEmbeddingTransport, *, query_instruction_policy="reject"):
        if not isinstance(transport, LocalEmbeddingTransport) or transport.dimension != 384 or transport.max_tokens != 256:
            raise ComparisonError("derived embeddings require the pinned 384-dimensional, 256-token local transport")
        if query_instruction_policy not in ("reject", "ignore"):
            raise ComparisonError("explicit query instruction policy must be reject or ignore")
        self.transport = transport
        self._identity_json = json.dumps({"encoder": transport.identity, "model": MODEL,
            "dimension": 384, "max_tokens": 256, "split_policy": SPLIT_POLICY,
            "pool_policy": POOL_POLICY, "query_instruction_policy": query_instruction_policy,
            "max_batch_utf8_bytes": 64 * 1024 * 1024,
            "max_text_characters": 2_000_000, "max_chunks_per_text": 8192}, sort_keys=True,
            ensure_ascii=False, separators=(",", ":"))

    @property
    def identity(self):
        return json.loads(self._identity_json)

    @property
    def representation_sha256(self):
        return hashlib.sha256(self._identity_json.encode()).hexdigest()

    async def __call__(self, texts: Sequence[str]):
        if (self.transport.identity != self.identity["encoder"] or self.transport.dimension != 384
                or self.transport.max_tokens != 256):
            raise ComparisonError("local encoder identity or representation window changed")
        if isinstance(texts, (str, bytes)) or not isinstance(texts, Sequence) or not 1 <= len(texts) <= 1024:
            raise ComparisonError("derived embedding batch must contain 1–1024 texts")
        if any(not isinstance(text, str) or not text.strip() or len(text) > 2_000_000 for text in texts):
            raise ComparisonError("bounded substantive derived text required")
        if sum(len(text.encode("utf-8")) for text in texts) > 64 * 1024 * 1024:
            raise ComparisonError("derived embedding batch exceeds aggregate UTF-8 budget")
        # Check pressure before expensive tokenization, and between long groups.
        groups = []
        for text in texts:
            admitted = self.transport.resource_guard()
            if inspect.isawaitable(admitted):
                admitted = await admitted
            if admitted is False:
                raise ComparisonError("local resource guard did not admit derived tokenization")
            groups.append(lossless_token_chunks(text, self.transport.token_count))
        async def encoded_chunks():
            # Fill bounded encoder batches across text boundaries. Stream the
            # results so pooling retains at most one text's vectors and one
            # 32-chunk batch, rather than materializing every input's vectors.
            batch = []
            for pieces in groups:
                for piece in pieces:
                    batch.append(piece)
                    if len(batch) == 32:
                        for vector in await self.transport(batch):
                            yield vector
                        batch = []
            if batch:
                for vector in await self.transport(batch):
                    yield vector

        output = []
        chunks = encoded_chunks()
        try:
            for pieces in groups:
                vectors = []
                for _ in pieces:
                    vectors.append(await anext(chunks))
                if len(vectors) == 1:
                    output.append(vectors[0])
                    continue
                mean = [math.fsum(row[index] for row in vectors) / len(vectors) for index in range(384)]
                norm = math.sqrt(math.fsum(value * value for value in mean))
                if not math.isfinite(norm) or norm == 0:
                    raise ComparisonError("derived chunk mean has no finite nonzero direction")
                output.append([value / norm for value in mean])
        finally:
            await chunks.aclose()
        return output


def _carry_representation(component, hook):
    """Carry immutable canonical identity bytes through native factory boundaries."""
    setter = setattr if isinstance(component, type) else object.__setattr__
    setter(component, "_comparison_embedding_hook", hook)
    setter(component, "comparison_representation_identity_json", hook._identity_json)
    setter(component, "comparison_representation_sha256", hook.representation_sha256)
    return component


def make_native_embedding(framework, hook: DerivedEmbeddingTransport, *, config=None):
    """Return the pinned native protocol; GraphRAG returns a registry initializer.

    Graphiti drivers and existing vector indexes must independently use dimension
    384; explicit EmbedderConfig does not override upstream global schema defaults.
    """
    if not isinstance(hook, DerivedEmbeddingTransport):
        raise ComparisonError("explicit derived local embedding transport required")
    if framework == "lightrag":
        import numpy as np
        from lightrag.utils import EmbeddingFunc
        async def embed(texts):
            return np.asarray(await hook(texts), dtype=np.float32)
        # None disables LightRAG's truncation BEFORE this lossless callback.
        # The underlying local transport still limits each actual chunk to 256.
        return _carry_representation(EmbeddingFunc(embedding_dim=384, max_token_size=None, model_name=MODEL, func=embed), hook)
    if framework == "graphiti":
        from graphiti_core.embedder.client import EmbedderClient, EmbedderConfig
        class ControlledEmbedder(EmbedderClient):
            def __init__(self):
                self.config = EmbedderConfig(embedding_dim=384)
            async def create(self, input_data):
                if isinstance(input_data, list) and len(input_data) == 1:
                    input_data = input_data[0]
                if not isinstance(input_data, str):
                    raise ComparisonError("Graphiti single embedding requires text; use create_batch for multiple texts")
                return (await hook([input_data]))[0]
            async def create_batch(self, input_data_list):
                return await hook(input_data_list)
        return _carry_representation(ControlledEmbedder(), hook)
    if framework == "hipporag2":
        import numpy as np
        from hipporag.embedding_model.base import BaseEmbeddingModel, EmbeddingConfig
        if config is None or config.embedding_model_name != MODEL:
            raise ComparisonError("explicit pinned HippoRAG embedding configuration required")
        class ControlledEmbedder(BaseEmbeddingModel):
            _comparison_batch_policy = HIPPORAG_BATCH_POLICY
            query_instruction_mode = "ignored" if hook.identity["query_instruction_policy"] == "ignore" else "reject"
            def __init__(self):
                super().__init__(config)
                self.embedding_dim = 384
                self.embedding_config = EmbeddingConfig.from_dict({"model": MODEL, "dimension": 384})
            def batch_encode(self, texts, **kwargs):
                if set(kwargs) - {"instruction", "norm"} or kwargs.get("norm", True) is not True:
                    raise ComparisonError("unsupported HippoRAG embedding options")
                if kwargs.get("instruction") and hook.identity["query_instruction_policy"] != "ignore":
                    raise ComparisonError("nonempty HippoRAG instruction requires explicit ignore policy for symmetric MiniLM")
                texts = [texts] if isinstance(texts, str) else texts
                if isinstance(texts, (str, bytes)) or not isinstance(texts, Sequence) or not texts:
                    raise ComparisonError("HippoRAG embedding requires text or a nonempty text sequence")
                # Native stores submit all missing entities at once. Validate
                # that logical request before inference, then keep each derived
                # call inside its existing count and byte limits.
                if any(not isinstance(text, str) or not text.strip() or len(text) > 2_000_000 for text in texts):
                    raise ComparisonError("bounded substantive derived text required")
                if sum(len(text.encode("utf-8")) for text in texts) > 64 * 1024 * 1024:
                    raise ComparisonError("derived embedding batch exceeds aggregate UTF-8 budget")
                async def embed_batches():
                    batches = []
                    for start in range(0, len(texts), 1024):
                        batches.append(np.asarray(await hook(texts[start:start + 1024]), dtype=np.float32))
                    return batches[0] if len(batches) == 1 else np.concatenate(batches, axis=0)
                return _sync(embed_batches())
        return _carry_representation(ControlledEmbedder(), hook)
    if framework == "llamaindex":
        from llama_index.core.base.embeddings.base import BaseEmbedding
        class ControlledEmbedder(BaseEmbedding):
            def _get_query_embedding(self, query):
                return _sync(hook([query]))[0]
            async def _aget_query_embedding(self, query):
                return (await hook([query]))[0]
            def _get_text_embedding(self, text):
                return _sync(hook([text]))[0]
            async def _aget_text_embedding(self, text):
                return (await hook([text]))[0]
            def _get_text_embeddings(self, texts):
                return _sync(hook(texts))
            async def _aget_text_embeddings(self, texts):
                return await hook(texts)
        return _carry_representation(ControlledEmbedder(model_name=MODEL, embed_batch_size=32), hook)
    if framework == "graphrag":
        from graphrag_llm.embedding.embedding import LLMEmbedding
        from graphrag_llm.types import LLMEmbeddingResponse
        class ControlledEmbedder(LLMEmbedding):
            def __init__(self, *, tokenizer, metrics_store, **kwargs):
                self._tokenizer, self._metrics_store = tokenizer, metrics_store
                _carry_representation(self, hook)
            def embedding(self, **kwargs):
                return _sync(self.embedding_async(**kwargs))
            async def embedding_async(self, **kwargs):
                if set(kwargs) - {"input", "dimensions", "encoding_format", "timeout"}:
                    raise ComparisonError("unsupported GraphRAG embedding options")
                if kwargs.get("dimensions", 384) not in (None, 384) or kwargs.get("encoding_format", "float") not in (None, "float"):
                    raise ComparisonError("GraphRAG requires 384-dimensional float embeddings")
                vectors = await hook(kwargs.get("input"))
                return LLMEmbeddingResponse.model_validate({"object": "list", "model": MODEL,
                    "data": [{"object": "embedding", "index": index, "embedding": vector} for index, vector in enumerate(vectors)],
                    "usage": {"prompt_tokens": 0, "total_tokens": 0}})
            @property
            def metrics_store(self):
                return self._metrics_store
            @property
            def tokenizer(self):
                return self._tokenizer
        return _carry_representation(ControlledEmbedder, hook)
    raise ComparisonError("unsupported native embedding protocol")
