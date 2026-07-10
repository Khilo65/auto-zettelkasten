from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

from . import ENGINE_VERSION

from .files import require_loopback_http_url


class ProviderError(RuntimeError):
    pass


class CloudPermissionError(ProviderError):
    pass


SECTION_KEYS = (
    "thesis",
    "method_and_research_design",
    "evidence_and_data",
    "detailed_findings",
    "strengths_and_contributions",
    "methodological_critique",
    "limitations",
    "what_this_source_can_support",
    "what_this_source_cannot_support",
    "locators",
)


def provider_from_name(name: str, model: str, *, allow_cloud: bool):
    normalized = name.strip().lower()
    if normalized == "deepseek":
        return DeepSeekReader(model=model, allow_cloud=allow_cloud)
    if normalized == "openrouter":
        return OpenRouterReader(model=model, allow_cloud=allow_cloud)
    if normalized == "gemini":
        return GeminiReader(model=model, allow_cloud=allow_cloud)
    if normalized == "ollama":
        return OllamaReader(model=model)
    raise ValueError(f"unknown reader provider: {name}")


def vision_provider_from_name(name: str, model: str, *, allow_cloud: bool):
    if name.strip().lower() != "gemini":
        raise ValueError("Gemini is the supported document-vision provider")
    return GeminiReader(model=model, allow_cloud=allow_cloud)


@dataclass(slots=True)
class _OpenAICompatibleReader:
    name: str
    model: str
    endpoint: str
    api_key_env: str
    allow_cloud: bool
    is_cloud: bool = True
    timeout: float = 180.0

    def read_source(
        self,
        text: str,
        metadata: Mapping[str, Any],
        question: str | None = None,
    ) -> Mapping[str, Any]:
        self._authorize()
        body = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": _system_prompt()},
                {"role": "user", "content": _source_prompt(text, metadata, question)},
            ],
        }
        payload = _post_json(
            self.endpoint,
            body,
            headers={"Authorization": f"Bearer {os.environ[self.api_key_env]}"},
            timeout=self.timeout,
        )
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"{self.name} returned an unexpected response") from exc
        return _parse_analysis(content)

    def _authorize(self) -> None:
        if not self.allow_cloud:
            raise CloudPermissionError(f"{self.name} requires explicit allow_cloud consent")
        if not os.getenv(self.api_key_env):
            raise ProviderError(f"{self.api_key_env} is not configured")


class DeepSeekReader(_OpenAICompatibleReader):
    def __init__(self, model: str = "deepseek-v4-flash", *, allow_cloud: bool = False) -> None:
        super().__init__(
            name="deepseek",
            model=model,
            endpoint="https://api.deepseek.com/chat/completions",
            api_key_env="DEEPSEEK_API_KEY",
            allow_cloud=allow_cloud,
        )


class OpenRouterReader(_OpenAICompatibleReader):
    def __init__(self, model: str, *, allow_cloud: bool = False) -> None:
        super().__init__(
            name="openrouter",
            model=model,
            endpoint="https://openrouter.ai/api/v1/chat/completions",
            api_key_env="OPENROUTER_API_KEY",
            allow_cloud=allow_cloud,
        )


@dataclass(slots=True)
class GeminiReader:
    model: str = "gemini-2.5-flash"
    allow_cloud: bool = False
    name: str = "gemini"
    is_cloud: bool = True
    timeout: float = 180.0

    def read_source(
        self,
        text: str,
        metadata: Mapping[str, Any],
        question: str | None = None,
    ) -> Mapping[str, Any]:
        return self._generate([{"text": _system_prompt() + "\n\n" + _source_prompt(text, metadata, question)}])

    def inspect_document(
        self,
        document: bytes,
        media_type: str,
        metadata: Mapping[str, Any],
        question: str | None = None,
    ) -> Mapping[str, Any]:
        parts = [
            {"text": _system_prompt() + "\n\n" + _metadata_prompt(metadata, question)},
            {"inline_data": {"mime_type": media_type, "data": base64.b64encode(document).decode("ascii")}},
        ]
        return self._generate(parts)

    def _generate(self, parts: list[dict[str, Any]]) -> Mapping[str, Any]:
        if not self.allow_cloud:
            raise CloudPermissionError("gemini requires explicit allow_cloud consent")
        key = os.getenv("GEMINI_API_KEY")
        if not key:
            raise ProviderError("GEMINI_API_KEY is not configured")
        endpoint = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{urllib.parse.quote(self.model, safe='')}:generateContent?key={urllib.parse.quote(key, safe='')}"
        )
        payload = _post_json(
            endpoint,
            {
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
            },
            timeout=self.timeout,
        )
        try:
            content = payload["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("gemini returned an unexpected response") from exc
        return _parse_analysis(content)


@dataclass(slots=True)
class OllamaReader:
    model: str = "llama3.2"
    base_url: str = "http://127.0.0.1:11434"
    name: str = "ollama"
    is_cloud: bool = False
    timeout: float = 300.0

    def __post_init__(self) -> None:
        require_loopback_http_url(self.base_url, label="Ollama base_url")

    def read_source(
        self,
        text: str,
        metadata: Mapping[str, Any],
        question: str | None = None,
    ) -> Mapping[str, Any]:
        payload = _post_json(
            f"{self.base_url.rstrip('/')}/api/chat",
            {
                "model": self.model,
                "stream": False,
                "format": "json",
                "messages": [
                    {"role": "system", "content": _system_prompt()},
                    {"role": "user", "content": _source_prompt(text, metadata, question)},
                ],
            },
            timeout=self.timeout,
        )
        try:
            content = payload["message"]["content"]
        except (KeyError, TypeError) as exc:
            raise ProviderError("ollama returned an unexpected response") from exc
        return _parse_analysis(content)


def _system_prompt() -> str:
    keys = ", ".join(SECTION_KEYS)
    return (
        "You create source-faithful atomic notes from inspected document content. "
        "Return only one JSON object. Do not infer facts absent from the source. "
        f"Every value must be a non-empty string. Required keys: {keys}. "
        "Detailed findings must retain concrete evidence, examples, statistics, and qualifications when present. "
        "Locators must identify pages, sections, headings, or explicit text anchors. "
        "If the source does not report something, say so explicitly instead of inventing it."
    )


def _metadata_prompt(metadata: Mapping[str, Any], question: str | None) -> str:
    safe_metadata = {
        key: metadata.get(key)
        for key in ("title", "creators", "date", "publicationTitle", "DOI", "url", "itemType")
        if metadata.get(key)
    }
    return f"Metadata: {json.dumps(safe_metadata, ensure_ascii=False)}\nQuestion lens: {question or 'none'}"


def _source_prompt(text: str, metadata: Mapping[str, Any], question: str | None) -> str:
    return f"{_metadata_prompt(metadata, question)}\n\nINSPECTED SOURCE CONTENT:\n{text}"


def _parse_analysis(value: Any) -> Mapping[str, Any]:
    if isinstance(value, dict):
        payload = value
    else:
        text = str(value).strip()
        fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, flags=re.DOTALL | re.IGNORECASE)
        if fenced:
            text = fenced.group(1)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError("reader response was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ProviderError("reader response must be a JSON object")
    missing = [key for key in SECTION_KEYS if not str(payload.get(key, "")).strip()]
    if missing:
        raise ProviderError(f"reader response omitted required sections: {', '.join(missing)}")
    return {key: str(payload[key]).strip() for key in SECTION_KEYS}


def _post_json(
    url: str,
    body: Mapping[str, Any],
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float,
) -> dict[str, Any]:
    request_headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": f"auto-zettelkasten/{ENGINE_VERSION}"}
    request_headers.update(headers or {})
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers=request_headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read(512).decode("utf-8", errors="replace")
        finally:
            exc.close()
        raise ProviderError(f"provider HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f"provider unavailable: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise ProviderError("provider response was not valid JSON") from exc
    if not isinstance(value, dict):
        raise ProviderError("provider response must be a JSON object")
    return value
