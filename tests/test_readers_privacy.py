from __future__ import annotations

import pytest

from auto_zettelkasten.readers import CloudPermissionError, DeepSeekReader, GeminiReader, OllamaReader, OpenRouterReader


@pytest.mark.parametrize(
    "reader",
    [
        DeepSeekReader(allow_cloud=False),
        OpenRouterReader("openai/gpt-4.1-mini", allow_cloud=False),
        GeminiReader(allow_cloud=False),
    ],
)
def test_cloud_adapters_fail_before_network_without_consent(reader) -> None:
    with pytest.raises(CloudPermissionError, match="explicit allow_cloud"):
        reader.read_source("inspected text", {"title": "Synthetic"})


def test_gemini_vision_fails_before_document_upload_without_consent() -> None:
    reader = GeminiReader(allow_cloud=False)
    with pytest.raises(CloudPermissionError, match="explicit allow_cloud"):
        reader.inspect_document(b"private-pdf", "application/pdf", {"title": "Private"})


def test_ollama_requires_loopback_endpoint() -> None:
    with pytest.raises(ValueError, match="loopback"):
        OllamaReader(base_url="http://192.0.2.10:11434")
