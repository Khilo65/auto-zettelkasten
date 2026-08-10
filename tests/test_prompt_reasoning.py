from __future__ import annotations

import json
from typing import Any

import pytest

from auto_zettelkasten.models import LiteratureMapRequest
from auto_zettelkasten.readers import (
    SECTION_KEYS,
    DeepSeekReader,
    OpenRouterReader,
    ProviderError,
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

    assert "cluster synthesis prompt v35" in prompt
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
    assert "acquisition_candidate_dispositions" in prompt
    assert "Do not generate a second independent recommendation list" in prompt
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
    reader.synthesize_cluster(
        [],
        LiteratureMapRequest(
            workspace=".",
            provider="deepseek",
            model="deepseek-v4-flash",
            allow_cloud=True,
        ),
        context={
            "cluster_id": "cluster-a",
            "_cluster_synthesis_reasoning_effort": "high",
        },
    )

    assert bodies[0]["thinking"] == {"type": "enabled"}
    assert bodies[0]["reasoning_effort"] == "high"
    assert "temperature" not in bodies[0]
    assert bodies[1]["thinking"] == {"type": "enabled"}
    assert bodies[1]["reasoning_effort"] == "max"
    assert "temperature" not in bodies[1]
    assert bodies[2]["thinking"] == {"type": "enabled"}
    assert bodies[2]["reasoning_effort"] == "high"
    assert "_cluster_synthesis_reasoning_effort" not in bodies[2]["messages"][1][
        "content"
    ]


def test_cluster_synthesis_fit_uses_exact_call_prompt_and_output_reserve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    fit_inputs: list[tuple[str, str, int]] = []
    bodies: list[dict[str, Any]] = []

    def prompt_fits(self, system_prompt, user_prompt, output_tokens, **kwargs):
        del self, kwargs
        fit_inputs.append((system_prompt, user_prompt, output_tokens))
        return True

    def post_json(endpoint, body, **kwargs):
        del endpoint, kwargs
        bodies.append(dict(body))
        return _completion(
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
        )

    monkeypatch.setattr(DeepSeekReader, "_prompt_fits", prompt_fits)
    monkeypatch.setattr("auto_zettelkasten.readers._post_json", post_json)
    reader = DeepSeekReader(allow_cloud=True)
    request = LiteratureMapRequest(
        workspace=".",
        provider="deepseek",
        model="deepseek-v4-flash",
        allow_cloud=True,
    )
    context = {"cluster": {"cluster_id": "cluster-a"}}

    assert reader.cluster_synthesis_fits([], request, context=context)
    assert bodies == []
    reader.synthesize_cluster([], request, context=context)

    system_prompt, user_prompt, output_tokens = fit_inputs[0]
    assert bodies[0]["messages"] == [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    assert bodies[0]["max_tokens"] == output_tokens == 128_000


def test_cluster_synthesis_fit_includes_exact_boundary() -> None:
    reader = DeepSeekReader(
        context_window_tokens=1_000_000,
        direct_read_fraction=1.0,
    )
    request = LiteratureMapRequest(
        workspace=".",
        provider="deepseek",
        model="deepseek-v4-flash",
    )
    context = {"cluster": {"cluster_id": "cluster-a"}}
    low = 1
    high = reader.context_window_tokens
    while low < high:
        midpoint = (low + high) // 2
        reader.context_window_tokens = midpoint
        if reader.cluster_synthesis_fits([], request, context=context):
            high = midpoint
        else:
            low = midpoint + 1

    reader.context_window_tokens = low
    assert reader.cluster_synthesis_fits([], request, context=context)
    reader.context_window_tokens = low - 1
    assert not reader.cluster_synthesis_fits([], request, context=context)


def test_cluster_partition_mode_uses_supplied_compact_cards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    bodies: list[dict[str, Any]] = []

    def post_json(endpoint, body, **kwargs):
        del endpoint, kwargs
        bodies.append(dict(body))
        return _completion(
            {
                "clusters": [
                    {
                        "cluster_id": "child-a",
                        "title": "First child",
                        "organizing_problem": "First bounded problem",
                        "coherence_rationale": "A and B answer it.",
                        "members": [
                            {
                                "source_id": "source-a",
                                "role": "core",
                                "membership_reason": "Directly answers it.",
                            },
                            {
                                "source_id": "source-b",
                                "role": "context",
                                "membership_reason": "Supplies its boundary.",
                            },
                        ],
                    },
                    {
                        "cluster_id": "child-b",
                        "title": "Second child",
                        "organizing_problem": "Second bounded problem",
                        "coherence_rationale": "C and D answer it.",
                        "members": [
                            {
                                "source_id": "source-c",
                                "role": "core",
                                "membership_reason": "Directly answers it.",
                            },
                            {
                                "source_id": "source-a",
                                "role": "bridge",
                                "membership_reason": "Connects the two problems.",
                            },
                        ],
                    },
                ],
                "neighbor_relationships": [],
                "unclustered_sources": [
                    {
                        "source_id": "source-d",
                        "reason": "It does not fit either bounded problem.",
                    }
                ],
            }
        )

    monkeypatch.setattr("auto_zettelkasten.readers._post_json", post_json)
    reader = DeepSeekReader(allow_cloud=True)
    request = LiteratureMapRequest(
        workspace=".",
        provider="deepseek",
        model="deepseek-v4-flash",
        allow_cloud=True,
    )
    parent_card = {
        "cluster_id": "cluster-parent",
        "organizing_problem": "How do distinct conflict stages relate?",
    }
    member_cards = [
        {"source_id": "source-a", "outcomes": ["onset"]},
        {"source_id": "source-b", "outcomes": ["recurrence"]},
        {"source_id": "source-c", "outcomes": ["duration"]},
        {"source_id": "source-d", "outcomes": ["settlement"]},
    ]

    response = reader.plan_clusters(
        [],
        request,
        context={
            "cluster_plan_mode": "partition",
            "compact_parent_cluster": parent_card,
            "compact_member_cards": member_cards,
        },
    )
    assert [row["cluster_id"] for row in response["clusters"]] == [
        "child-a",
        "child-b",
    ]

    payload = json.loads(bodies[0]["messages"][1]["content"])
    assert payload["profiles"] == []
    assert payload["context"]["compact_parent_cluster"] == parent_card
    assert payload["context"]["compact_member_cards"] == member_cards
    assert "partition the supplied compact parent cluster card" in payload[
        "instruction"
    ]
    assert "partition policy v3" in payload["instruction"]
    assert "never by token size alone" in payload["instruction"]
    assert "exact member roles core, context, or bridge" in payload["instruction"]
    assert "exactly one primary child" in payload["instruction"]
    assert "or place it in unclustered_sources" in payload["instruction"]
    assert "additional memberships are bridge only" in payload["instruction"]
    assert "Do not repeat source cards" in payload["instruction"]


@pytest.mark.parametrize(
    ("mutate", "diagnostic"),
    [
        ("bridge_only", "bridge_only_source_ids=source-d"),
        ("duplicate_primary", "duplicate_primary_source_ids=source-a"),
        ("invalid_role", "invalid_role_memberships=source-d:supporting"),
    ],
)
def test_cluster_partition_contract_reports_source_ids_without_retry(
    monkeypatch: pytest.MonkeyPatch,
    mutate: str,
    diagnostic: str,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    bodies: list[dict[str, Any]] = []
    clusters = [
        {
            "cluster_id": "child-a",
            "title": "First child",
            "organizing_problem": "First bounded problem",
            "members": [
                {"source_id": "source-a", "role": "core"},
                {"source_id": "source-b", "role": "context"},
            ],
        },
        {
            "cluster_id": "child-b",
            "title": "Second child",
            "organizing_problem": "Second bounded problem",
            "members": [
                {"source_id": "source-c", "role": "core"},
                {"source_id": "source-d", "role": "context"},
            ],
        },
    ]
    if mutate == "bridge_only":
        clusters[1]["members"][1]["role"] = "bridge"
    elif mutate == "duplicate_primary":
        clusters[1]["members"].append({"source_id": "source-a", "role": "context"})
    else:
        clusters[1]["members"][1]["role"] = "supporting"

    def post_json(endpoint, body, **kwargs):
        del endpoint, kwargs
        bodies.append(dict(body))
        return _completion(
            {
                "clusters": clusters,
                "neighbor_relationships": [],
                "unclustered_sources": [],
            }
        )

    monkeypatch.setattr("auto_zettelkasten.readers._post_json", post_json)
    reader = DeepSeekReader(allow_cloud=True)
    request = LiteratureMapRequest(
        workspace=".",
        provider="deepseek",
        model="deepseek-v4-flash",
        allow_cloud=True,
    )
    context = {
        "cluster_plan_mode": "partition",
        "compact_parent_cluster": {
            "cluster_id": "cluster-parent",
            "source_ids": ["source-a", "source-b", "source-c", "source-d"],
        },
        "compact_member_cards": [
            {"source_id": source_id}
            for source_id in ("source-a", "source-b", "source-c", "source-d")
        ],
    }

    with pytest.raises(ProviderError, match=diagnostic) as raised:
        reader.plan_clusters([], request, context=context)

    assert len(bodies) == 1
    assert getattr(raised.value, "raw_response", {})["clusters"] == clusters


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
