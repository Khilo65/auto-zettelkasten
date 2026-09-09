from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from auto_zettelkasten.models import LiteratureMapRequest
from auto_zettelkasten.pipeline import _indexed_pdf_text_with_page_markers, _split_document
from auto_zettelkasten.readers import (
    SECTION_KEYS,
    CodexReader,
    DeepSeekReader,
    OpenRouterReader,
    ProviderError,
    _chunk_prompt,
    _chunk_system_prompt,
    _cluster_synthesis_system_prompt,
    _gap_adjudication_system_prompt,
    _source_bundle_prompt,
    _source_bundle_system_prompt,
    _source_prompt,
    _system_prompt,
    codex_contract_identity,
)
from auto_zettelkasten.relationships import RELATIONSHIP_DISCOVERY_PROMPT_VERSION


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


def test_source_bundle_filters_cached_pdf_initials_without_losing_html_headings() -> None:
    fragment = "M. R. Vale (2018) described the measurement"
    spans = [
        {"label": "I. Overview", "page_ordinal": 1, "printed_page": "3"},
        {"label": fragment, "page_ordinal": 2, "printed_page": "4"},
        {"label": fragment},  # Explicit HTML headings have no PDF ordinal.
    ]
    metadata = {"_source_context": {"heading_spans": spans}}
    before = json.dumps(metadata)
    prompt = _source_bundle_prompt("Unchanged source content.", metadata, None)
    context = json.loads(prompt.splitlines()[0].split(": ", 1)[1])

    assert context["heading_spans"] == [spans[0], spans[2]]
    assert json.dumps(metadata) == before
    assert "Unchanged source content." in prompt


def test_atomic_prompt_v14_is_source_adaptive_and_statistics_aware() -> None:
    prompt = _system_prompt()

    assert "atomic prompt v14" in prompt
    assert "optional key_concepts_and_definitions" in prompt
    assert "omit key_concepts_and_definitions entirely" in prompt
    assert "optional source_structure_and_organization" in prompt
    assert "short source-native navigation outline, not an argument map" in prompt
    assert (
        "include major headings and only consequential first-level subheadings"
        in prompt
    )
    assert "For a partial source, label the outline partial" in prompt
    assert "Do not infer missing headings" in prompt
    assert "Omit source_structure_and_organization entirely" in prompt
    assert "short exact quotation" in prompt
    assert "page number when supplied" in prompt
    assert "source-grounded paraphrase as a paraphrase" in prompt
    assert "never invent a page number" in prompt
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


def test_source_bundle_v40_requests_detailed_source_content_without_claim_inventory():
    system = _source_bundle_system_prompt()
    prompt = _source_bundle_prompt("Original source text.", {}, None)
    assert "source bundle prompt v40" in system
    for content in ("thesis", "methods", "evidence and data", "examples", "qualifications",
                    "author or speaker", "marked footnotes", "page, chapter", "exact quotations",
                    "compact_profile", "literature_positions", "recoverable"):
        assert content in system
    assert "evidence_anchors" not in system + prompt
    assert "quantitative_result" not in system + prompt
    assert "FINAL QUANTITATIVE COPY GATE" not in prompt
    assert "Original source text." in prompt






def test_cluster_synthesis_requires_clause_support_not_just_source_ownership() -> None:
    prompt = _cluster_synthesis_system_prompt()

    assert "Ground each finding in its source's supplied atomic note" in prompt
    assert "Omit or narrow an unsupported clause" in prompt
    assert "anchor" not in prompt
    assert "Each study finding is about exactly one source" in prompt
    assert "Put cross-source comparisons in the line's synthesis" in prompt




















def test_chunk_prompt_preserves_quote_and_chapter_start_boundaries() -> None:
    prompt = _chunk_system_prompt()

    assert "contiguous verbatim quotation without inserted ellipses" in prompt
    assert "labeled source-grounded paraphrase" in prompt
    assert "actual opening heading" in prompt
    assert "mark a continuation and omit its start" in prompt
    assert "not a section boundary" in prompt
    assert "authors, publication years, and titles" in prompt
    assert "prioritize substantively engaged works" in prompt
    assert "Do not copy an unengaged bibliography" in prompt
    assert "leave unknown metadata empty" in _source_bundle_system_prompt()


def test_chunk_final_check_preserves_pdf_and_printed_coordinates() -> None:
    source = "--- Page 42 ---\n32\nFindings\nThe effect was conditional."
    prompt = _chunk_prompt(
        source, {"_source_context": {"ordinal_to_printed_page": {"42": "32"}}},
        None, "chunk-0001", "pages 42-44",
    )
    final = prompt.split("FINAL LOCATOR CHECK:")[1]

    annotated = "--- Page 42 ---\n[Citation locator: p. 32]\n32\nFindings\nThe effect was conditional."
    assert annotated in prompt
    assert prompt.index(annotated) < prompt.index(final)
    assert annotated.replace("\n[Citation locator: p. 32]", "") == source
    assert "one supporting page coordinate per citation" in final
    assert "otherwise cite the physical marker as PDF p./pp." in final
    assert "Do not output both coordinates or infer a missing printed label" in final
    assert "Copy the adjacent Citation locator line verbatim" in final
    assert "this line is navigation, not source evidence" in final
    assert "Preserve explicitly credited authors and speakers" in final
    assert "each section opening" in final
    assert "supporting locator directly to each retained claim, result, and cited work" in final
    assert "Keep investigators distinct from the subjects they investigate" in final
    assert "documents examined by a study" in final
    assert "exact measured outcome and its qualifiers" in final
    assert "not a neighboring comparison" in final
    assert "absence of effect on other outcomes" in final


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
@pytest.mark.parametrize("page_map", [{"1": "12", "2": "13"}, {}], ids=["printed", "pdf"])
def test_chunk_prompt_annotates_preserved_indexed_pdf_line_endings(
    newline: str, page_map: dict[str, str],
) -> None:
    source = newline.join([
        "--- Page 1 ---", "First passage.", "", "--- Page 2 ---", "Second passage.",
    ])
    indexed = _indexed_pdf_text_with_page_markers(
        source, {"indexedPages": 2, "totalPages": 2},
    )
    assert indexed == source
    chunk, = _split_document(indexed, chunk_char_limit=1000)
    assert chunk.split("\n", 1)[1].encode() == source.encode()
    metadata = {"_source_context": {"ordinal_to_printed_page": page_map}}
    before = json.dumps(metadata)
    prompt = _chunk_prompt(chunk, metadata, None, "chunk-0001", "pages 1-2")
    annotated = chunk
    for ordinal in ("1", "2"):
        locator = f"p. {page_map[ordinal]}" if ordinal in page_map else f"PDF p. {ordinal}"
        marker = f"--- Page {ordinal} ---"
        annotated = annotated.replace(marker, f"{marker}\n[Citation locator: {locator}]")
    assert annotated in prompt
    assert prompt.count("[Citation locator:") == 2
    restored = annotated
    for ordinal in ("1", "2"):
        locator = f"p. {page_map[ordinal]}" if ordinal in page_map else f"PDF p. {ordinal}"
        restored = restored.replace(f"\n[Citation locator: {locator}]", "")
    assert restored.encode() == chunk.encode()
    assert json.dumps(metadata) == before























def test_final_source_prompt_schema_and_contract_hashes_are_frozen() -> None:
    def digest(value: str | dict[str, Any]) -> str:
        text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    bundle_identity = codex_contract_identity(
        "source_bundle", "gpt-5.6-luna", "medium"
    )
    chunk_identity = codex_contract_identity(
        "chunk_evidence", "gpt-5.6-luna", "medium"
    )
    assert {
        "atomic_prompt_v14": digest(_system_prompt()),
        "chunk_prompt_bundle_v40": digest(_chunk_system_prompt()),
        "chunk_user_prompt_bundle_v40": digest(
            _chunk_prompt("A fictional source.", {}, None, "chunk-0001", "pages 1-2")
        ),
        "source_bundle_prompt_v40": digest(_source_bundle_system_prompt()),
        "source_bundle_user_prompt_v40": digest(
            _source_bundle_prompt("A fictional source.", {}, None)
        ),
        "codex_source_bundle_schema": bundle_identity["schema_hash"],
        "codex_source_bundle_contract": digest(bundle_identity),
        "codex_chunk_evidence_schema": chunk_identity["schema_hash"],
        "codex_chunk_evidence_contract": digest(chunk_identity),
    } == {
        "atomic_prompt_v14": "8db9f2990d175816cb0100b92d84734ae7c2f930aade66825ed0412b22da3705",
        "chunk_prompt_bundle_v40": "1ef440d8148d4a58491ac2a74cf9d65c0d52f9f4d2e42278f1961e94e570dea2",
        "chunk_user_prompt_bundle_v40": "13825c29551703fdc760ab0ad496ac3210658dfe290c36fa027f6c51f3d72a05",
        "source_bundle_prompt_v40": "95ce1b464908e1c169b7f2ff71620832e758a8e44d380fa49e999f9d7a0bb1bd",
        "source_bundle_user_prompt_v40": "549c46f156b10241178c9bf7401b56ddcef778b5465bf7325aa14e25dfc8ae1e",
        "codex_source_bundle_schema": "1f0ff3c8a6b465335c254aee9ea4096e65d3e76aea53daf94eb2c5d25f855b72",
        "codex_source_bundle_contract": "a9b70cb5a6df925b964543d8bdfcdef2e872655b578db9702aaf96191d9ec272",
        "codex_chunk_evidence_schema": "130ebe184fc8dc0b3c08879abfeb0435500a47eecd78ed7e2387c095574d821c",
        "codex_chunk_evidence_contract": "14f169c8b8c4d0a86004198198d4c40f93ac911884e22a25dc0acde24dbf1035",
    }


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

    assert "cluster synthesis prompt v42" in prompt
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


def test_cluster_prompt_preserves_observation_windows_without_excluding_context() -> None:
    from auto_zettelkasten.literature import _synthesis_stage_prompt_version

    prompt = _cluster_synthesis_system_prompt()
    assert _synthesis_stage_prompt_version("cluster_synthesis") == "42"
    assert "Preserve each source's observation period in cross-source comparisons" in prompt
    assert "explicitly dated context, not contemporaneous evidence" in prompt
    assert "different instruments, samples, or time windows" in prompt
    assert "every retained complete note, not just the selected study findings" in prompt
    assert "Exclusive claims must name the compared outcome or contribution" in prompt
    assert "when relevant, the observation window that defines the comparison" in prompt
    assert "otherwise remove the exclusivity" in prompt


def test_gap_prompt_rejects_invented_resolution_details() -> None:
    prompt = _gap_adjudication_system_prompt()

    assert "gap prompt v12" in prompt
    assert "Do not invent named cases, datasets, instruments" in prompt


@pytest.mark.parametrize("provider", ["codex", "deepseek"])
def test_ordinary_relationship_request_defines_sequence_direction(
    monkeypatch: pytest.MonkeyPatch, provider: str,
) -> None:
    reader = (
        CodexReader(model="gpt-5.6-terra", allow_cloud=True)
        if provider == "codex" else DeepSeekReader(allow_cloud=True)
    )
    prompts: list[str] = []

    def literature_json_call(system_prompt, user_prompt, **kwargs):
        prompts.append(system_prompt)
        assert kwargs["contract_id"] == "relationship_candidate_selection"
        return {"candidates": [], "job_outcomes": []}

    monkeypatch.setattr(reader, "_literature_json_call", literature_json_call)
    reader.select_relationship_candidates(
        [], LiteratureMapRequest(
            workspace=".", provider=provider, model=reader.model, allow_cloud=True,
        ),
    )

    assert len(prompts) == 1
    assert f"ordinary relationship prompt v{RELATIONSHIP_DISCOVERY_PROMPT_VERSION}" in prompts[0]
    assert "sequential_relationship means the actor precedes the reference" in prompts[0]
    assert "Citation or chronology alone establishes neither support nor direction" in prompts[0]


def test_legacy_relationship_request_supplies_connection_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    bodies: list[dict[str, Any]] = []

    def post_json(endpoint, body, **kwargs):
        del endpoint, kwargs
        bodies.append(body)
        return _completion({"decisions": []})

    monkeypatch.setattr("auto_zettelkasten.readers._post_json", post_json)
    DeepSeekReader(allow_cloud=True).adjudicate_relationships(
        [],
        LiteratureMapRequest(
            workspace=".", provider="deepseek", model="deepseek-v4-flash",
            allow_cloud=True,
        ),
    )

    assert len(bodies) == 1
    assert bodies[0]["response_format"] == {"type": "json_object"}
    prompt = " ".join(message["content"] for message in bodies[0]["messages"])
    for field in (
        "comparison_proposition", "primary_relation_type", "secondary_relation_types",
        "actor_source_id", "reference_source_id", "source_a_basis", "source_b_basis",
        "reason", "boundary_or_qualification", "confidence",
    ):
        assert field in prompt


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


@pytest.mark.parametrize("alias", ["_source_context", "source_context", "extraction_provenance", "extraction"])
@pytest.mark.parametrize("page_map, expected", [
    ({"2": "17"}, "p. 17"),
    ({2: 17}, "p. 17"),
    ({"2": " iv "}, "p. iv"),
    ({"2": "IX"}, "p. IX"),
    ({"2": "17", "9": "17"}, "PDF p. 2"),
    ({"2": "iv", "9": "IV"}, "PDF p. 2"),
    ({"2": "17", 2: "18"}, "PDF p. 2"),
    ({"2": ""}, "PDF p. 2"),
    ({"2": None}, "PDF p. 2"),
    ({"2": []}, "PDF p. 2"),
    ({"2": True}, "PDF p. 2"),
    ({"2": "A-17"}, "PDF p. 2"),
    ({"2": "17\nOther text"}, "PDF p. 2"),
    ({}, "PDF p. 2"),
    ([], "PDF p. 2"),
    ("17", "PDF p. 2"),
    (None, "PDF p. 2"),
])
def test_chunk_prompt_annotations_use_metadata_aliases_without_mutation(
    alias: str, page_map: Any, expected: str,
) -> None:
    source = "--- Page 2 ---\nFirst body.\n\n--- Page 3 ---\nSecond body."
    metadata = {alias: {"ordinal_to_printed_page": page_map}}
    before = json.dumps(metadata)
    prompt = _chunk_prompt(source, metadata, None, "chunk-0001", "pages 2-3")
    annotated = source.replace("--- Page 2 ---", f"--- Page 2 ---\n[Citation locator: {expected}]")
    annotated = annotated.replace("--- Page 3 ---", "--- Page 3 ---\n[Citation locator: PDF p. 3]")

    assert annotated in prompt
    assert annotated.replace(f"\n[Citation locator: {expected}]", "").replace(
        "\n[Citation locator: PDF p. 3]", "",
    ) == source
    assert json.dumps(metadata) == before


def test_chunk_prompt_context_precedence_matches_metadata_and_leaves_html_unmarked() -> None:
    source = "--- Page 2 ---\nBody."
    metadata = {
        "_source_context": [],
        "source_context": {"ordinal_to_printed_page": {"2": "iv"}},
        "extraction": {"ordinal_to_printed_page": {"2": "17"}},
    }
    assert "--- Page 2 ---\n[Citation locator: p. iv]\nBody." in _chunk_prompt(
        source, metadata, None, "", "",
    )
    metadata["_source_context"] = {}
    assert "--- Page 2 ---\n[Citation locator: PDF p. 2]\nBody." in _chunk_prompt(
        source, metadata, None, "", "",
    )
    html = "<h2>Results</h2><p>Quoted marker --- Page 2 --- stays in prose.</p>"
    prompt = _chunk_prompt(html, metadata, None, "", "")
    assert html in prompt
    assert "[Citation locator:" not in prompt
