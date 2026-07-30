from __future__ import annotations

import json
from typing import Any

import pytest

from auto_zettelkasten.models import LiteratureMapRequest
from auto_zettelkasten.readers import (
    SECTION_KEYS,
    DeepSeekReader,
    OpenRouterReader,
    _cluster_synthesis_system_prompt,
    _gap_adjudication_system_prompt,
    _source_prompt,
    _system_prompt,
)


def _analysis() -> dict[str, str]:
    return {key: f"value for {key}" for key in SECTION_KEYS}


def _completion(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": json.dumps(payload)},
            }
        ]
    }


def test_atomic_prompt_v11_is_source_adaptive_and_statistics_aware() -> None:
    prompt = _system_prompt()

    assert "atomic prompt v11" in prompt
    assert "blog post" in prompt
    assert "conference or meeting record" in prompt
    assert "findings, arguments, observations, interpretations, or recommendations" in prompt
    assert "method or knowledge basis" in prompt
    assert "case or conflict, actors, population" in prompt
    assert "what the method and evidence can actually establish" in prompt
    assert "descriptive before-and-after arithmetic as an identified causal effect" in prompt
    assert "different population estimates used in the arithmetic" in prompt
    assert "process-tracing arguments" in prompt
    assert "do not force fixed labels" in prompt
    assert "observed events and reported numbers" in prompt
    assert "what the design cannot rule out" in prompt
    assert "9 percentage points lower" in prompt
    assert "22.5% lower relative" in prompt
    assert "logit coefficient is not a probability change" in prompt
    assert "p-value is not an effect size" in prompt
    assert "hypothetical populations" in prompt
    assert "descriptive arithmetic, not an estimated causal effect" in prompt
    assert "approximately half-million Tutsi estimate" not in prompt
    assert "500-fold and 6,000-fold" not in prompt
    assert "Observed sequence:" not in prompt
    assert "PDF extraction may flatten tables" in prompt
    assert "never invent an exact row-column relationship" in prompt
    assert "silently reread" in prompt


def test_source_prompt_includes_only_compact_extraction_context() -> None:
    prompt = _source_prompt(
        "--- Page 1 ---\nEvidence.",
        {
            "title": "A source",
            "_source_context": {
                "source_type": "actual_pdf",
                "coverage": "full_document",
                "route": "pypdf_with_page_ocr",
                "page_count": 12,
                "embedded_text_page_count": 10,
                "ocr_page_count": 2,
                "unresolved_pages": [],
                "internal_fingerprint": "must-not-enter-prompt",
            },
        },
        None,
    )

    assert '"source_type": "actual_pdf"' in prompt
    assert '"page_count": 12' in prompt
    assert '"ocr_page_count": 2' in prompt
    assert "internal_fingerprint" not in prompt


def test_partial_source_prompt_prohibits_complete_document_inference() -> None:
    prompt = _source_prompt(
        "--- Page 1 ---\nAvailable evidence.",
        {
            "title": "A partial source",
            "_source_context": {
                "source_scope": "partial_document",
                "unresolved_pages": [2],
            },
        },
        None,
    )

    assert "PARTIAL-SOURCE RULE" in prompt
    assert "do not infer the complete thesis" in prompt


def test_cluster_prompt_preserves_inference_and_case_evidence() -> None:
    prompt = _cluster_synthesis_system_prompt()

    assert "cluster synthesis prompt v28" in prompt
    assert "Read every supplied atomic_note_markdown" in prompt
    assert "Every retained member" in prompt
    assert "specific study finding" in prompt
    assert "not generic thematic boilerplate" in prompt
    assert "observational, descriptive" in prompt
    assert "percentage-point versus relative percentage" in prompt
    assert "p-value is not an effect size" in prompt
    assert "numerator and denominator" in prompt
    assert "named-source attribution" in prompt
    assert "do not create cross-study conversions" in prompt
    assert "practitioner recommendation" in prompt


def test_gap_prompt_rejects_invented_resolution_details() -> None:
    prompt = _gap_adjudication_system_prompt()

    assert "gap prompt v12" in prompt
    assert "Do not invent named cases, datasets, instruments" in prompt


def test_deepseek_atomic_and_cluster_calls_use_requested_thinking_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    bodies: list[dict[str, Any]] = []

    def post_json(endpoint, body, **kwargs):
        del endpoint, kwargs
        bodies.append(dict(body))
        system_prompt = body["messages"][0]["content"]
        payload = (
            {
                "cluster_id": "cluster-a",
                "status": "accepted",
                "title": "A cluster",
                "organizing_mode": "question",
                "organizing_problem": "What does the literature establish?",
                "bottom_line": "The supplied source provides one bounded finding.",
                "lines_of_inquiry": [],
                "differences": [],
                "limits": [],
                "related_clusters": [],
                "retained_member_ids": [],
                "dropped_members": [],
                "missing_member_ids": [],
            }
            if "full-note cluster writer" in system_prompt
            else _analysis()
        )
        return _completion(payload)

    monkeypatch.setattr("auto_zettelkasten.readers._post_json", post_json)
    reader = DeepSeekReader(allow_cloud=True)

    reader.read_source("--- Page 1 ---\nEvidence.", {"title": "A source"})
    reader.synthesize_cluster(
        [],
        LiteratureMapRequest(
            workspace=".",
            provider="deepseek",
            model="deepseek-v4-flash",
            allow_cloud=True,
        ),
        context={"cluster_id": "cluster-a"},
    )

    assert bodies[0]["thinking"] == {"type": "enabled"}
    assert bodies[0]["reasoning_effort"] == "high"
    assert "temperature" not in bodies[0]
    assert bodies[1]["thinking"] == {"type": "enabled"}
    assert bodies[1]["reasoning_effort"] == "max"
    assert "temperature" not in bodies[1]


def test_generic_openai_provider_keeps_deterministic_nonreasoning_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    bodies: list[dict[str, Any]] = []

    def post_json(endpoint, body, **kwargs):
        del endpoint, kwargs
        bodies.append(dict(body))
        return _completion(_analysis())

    monkeypatch.setattr("auto_zettelkasten.readers._post_json", post_json)
    OpenRouterReader("provider/model", allow_cloud=True).read_source(
        "source text", {"title": "A source"}
    )

    assert bodies[0]["temperature"] == 0
    assert "thinking" not in bodies[0]
    assert "reasoning_effort" not in bodies[0]
