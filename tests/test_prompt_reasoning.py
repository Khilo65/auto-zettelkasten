from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from auto_zettelkasten.models import LiteratureMapRequest
from auto_zettelkasten.readers import (
    SECTION_KEYS,
    DeepSeekReader,
    OpenRouterReader,
    ProviderError,
    _chunk_system_prompt,
    _cluster_synthesis_system_prompt,
    _gap_adjudication_system_prompt,
    _source_bundle_prompt,
    _source_bundle_system_prompt,
    _source_prompt,
    _system_prompt,
    codex_contract_identity,
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


def test_source_bundle_prompt_v32_preserves_formatting_and_attribution_scope() -> None:
    prompt = _source_bundle_system_prompt()

    assert "source bundle prompt v32" in prompt
    assert "apply a footnote, only when the alignment or marker is explicit" in prompt
    assert "A footnote qualifies only the values bearing its explicit marker" in prompt
    assert "page metadata is not a statistic's observation date" in prompt
    assert "omit the year rather than borrowing it from page metadata" in prompt
    assert "Every numeric date or year endpoint" in prompt
    assert "never attach a global conflict or study start date" in prompt
    assert "Put a derived number only in estimate" in prompt
    assert "Every numeric statistic" in prompt
    assert "do not expand them into newly calculated full integers" in prompt
    assert "Do not infer a subgroup claim from an aggregate count" in prompt
    assert "singular or plural cardinality" in prompt
    assert "do not infer a group's position" in prompt
    assert "selected journalistic examples are not a survey" in prompt
    assert "Each literature-position row represents exactly one distinct work" in prompt
    assert "operative dates, deadlines, effective dates, and signing dates" in prompt
    assert 'Quote "exact unique source words"' in prompt
    assert "12–120 characters" in prompt
    assert "Descriptive paragraph labels are not locators" in prompt
    assert "physical PDF ordinals" in prompt
    assert "Reserve bare `p. N`" in prompt


def test_source_bundle_preserves_coverage_lineage_and_prose_scope() -> None:
    system = _source_bundle_system_prompt()
    prompt = _source_bundle_prompt("A fictional source.", {}, None)

    assert "canonical dataset title and explicitly stated edition" in system
    assert "hosting site or presentation format" in system
    assert "located anchors for central mechanisms and author interpretations" in system
    assert "resulting rank and its rank change" in prompt
    assert "In every analysis section and compact_profile, a footnote still qualifies only its explicitly marked measure" in prompt
    assert "explicitly marked footnote's temporal scope in period" in prompt
    assert "never erase a required marked-footnote scope" in prompt
    assert "numeric optional field other than period" in prompt
    assert "table row or column header" in prompt
    assert "Preserve the selected table year in period" in prompt
    assert "never erase a required table-year scope" in prompt


def test_cluster_synthesis_requires_clause_support_not_just_source_ownership() -> None:
    prompt = _cluster_synthesis_system_prompt()

    assert "Each cited anchor must support the attached finding" in prompt
    assert "Omit or narrow an unsupported clause" in prompt
    assert "same-source anchor about a different finding" in prompt
    assert "Each study finding is about exactly one source" in prompt
    assert "every evidence object's source_id must equal that study finding's source_id" in prompt
    assert "Put cross-source comparisons in the line's synthesis" in prompt


def test_source_bundle_prompt_keeps_numeric_values_out_of_statistic_labels() -> None:
    prompt = _source_bundle_system_prompt()

    assert "statistic names the reported measure or statistic type" in prompt
    assert "Do not repeat or concatenate numeric estimates in statistic" in prompt


def test_source_bundle_prompt_splits_distinct_quantitative_observations() -> None:
    prompt = _source_bundle_system_prompt()

    assert "Use one quantitative_result for one observation" in prompt
    assert "split them into separate evidence anchors" in prompt
    assert "Do not join distinct observation dates with semicolons" in prompt


def test_source_bundle_prompt_requires_a_final_quantitative_copy_gate() -> None:
    prompt = _source_bundle_prompt("The source reports 42 percent.", {}, None)

    assert "FINAL QUANTITATIVE COPY GATE" in prompt
    assert "One quantitative_result is one observation" in prompt
    assert "split distinct outcomes, categories" in prompt
    assert "Overall rank, sub-index score, and sub-index rank" in prompt
    assert "Sample size and geographic coverage are not components" in prompt
    assert "A prose sentence or list is not a joint statistic" in prompt
    assert "Never copy a footnote period or qualifier" in prompt
    assert "omit a lower-salience result instead of combining observations" in prompt
    assert "same source sentence explicitly binds the same number and noun" in prompt
    assert 'set that optional string to ""' in prompt
    assert "Do not copy study-level sample or coverage" in prompt
    assert "Set quantitative_result to null only when estimate fails" in prompt
    assert "INSPECTED SOURCE CONTENT" in prompt
    assert prompt.index("INSPECTED SOURCE CONTENT") < prompt.index(
        "FINAL QUANTITATIVE COPY GATE"
    )


def test_source_bundle_prompt_illustrates_measure_specific_footnote_scope() -> None:
    prompt = _source_bundle_prompt("A fictional source.", {}, None)

    assert "Illustration, not source evidence" in prompt
    assert "measure A; measure B*" in prompt
    assert "only B inherits period P and organization Q" in prompt
    assert "A inherits neither from that footnote" in prompt
    assert "retain independently source-stated observation periods" in prompt


def test_source_bundle_prompt_separates_qualitative_classifications_from_counts() -> None:
    prompt = _source_bundle_prompt("A fictional source.", {}, None)

    assert "Keep qualitative classifications such as 'one of the most ...'" in prompt
    assert "separate nonquantitative evidence anchor or source-grounded analysis prose" in prompt
    assert "retain genuine one-person, one-item, or per-unit counts numerically" in prompt


def test_source_bundle_final_review_covers_prose_citations_and_locators() -> None:
    source = (
        "Regional total: 90; North: 60; South: 30. "
        "Scores: access 8, trust 7, reach 6. "
        "Rates: 2 deliveries*; 3 visits. *First week only. "
        "Opening: Ada criticizes restrictions. Closing: Ben defends access. "
        "Journal Q published a 2001 study and a separate undated commentary."
    )
    prompt = _source_bundle_prompt(source, {}, None)
    final = prompt.split("FINAL WHOLE-SOURCE CHECK:")[1]

    assert prompt.index(source) < prompt.index("FINAL QUANTITATIVE COPY GATE")
    assert prompt.index("FINAL QUANTITATIVE COPY GATE") < prompt.index(final)
    for requirement in (
        "all analysis sections, compact_profile, evidence_anchors, and literature_positions",
        "Critiques, superlatives, contrasts, and locators are factual claims too",
        "population, period, measure, and category",
        "totals and subtotals",
        "all comparable displayed values",
        "aligned speakers",
        "every attributed quotation, paraphrase, and definition",
        "local reporting clause",
        "key_concepts_and_definitions",
        "do not carry a neighboring speaker",
        "explicit text anchors",
        "one distinct work",
        "unknown years and titles empty",
        "marked measure in every field",
        "same call",
    ):
        assert requirement in final
    assert "three to eight" not in _source_bundle_system_prompt()


def test_source_bundle_critique_distinguishes_totals_from_component_scopes() -> None:
    prompt = _source_bundle_system_prompt()

    assert "A total and a geographic or demographic subset are not competing estimates" in prompt
    assert "including subtotals omitted from your evidence anchors" in prompt
    assert "same population, period, measure, and category" in prompt
    assert "Do not invent a source-quality criticism" in prompt


def test_source_bundle_checks_repeated_dates_and_temporal_field_roles() -> None:
    source = "The 2019 report describes expansion from 30 to 40 regions in 2017."
    final = _source_bundle_prompt(source, {}, None).split("FINAL WHOLE-SOURCE CHECK:")[1]

    assert "Distinguish the date of a design, sample, or instrument change from the report edition" in final
    assert "Use the source's change date consistently across every section" in final
    assert "a temporal window belongs in period, not sample or uncertainty" in final
    assert "retain independently source-stated observation periods" in final
    assert "leave unmarked sibling observations unscoped" not in final


def test_source_bundle_checks_comparative_score_and_rank_scope() -> None:
    source = "Access: score 7, rank 80. Reach: score 6, rank 20."
    final = _source_bundle_prompt(source, {}, None).split("FINAL WHOLE-SOURCE CHECK:")[1]

    assert "relative strengths and weaknesses must name their comparison set" in final
    assert "a weak cross-entity rank does not imply a low within-entity score" in final
    assert "Do not interchange score and rank" in final


def test_source_bundle_keeps_rated_target_in_grouped_prose() -> None:
    source = "Region A rated service X lower. Region B rated service Y lower."
    final = _source_bundle_prompt(source, {}, None).split("FINAL WHOLE-SOURCE CHECK:")[1]

    assert "preserve who is measured and what or whom they are rating" in final
    assert "Split a summary list when its examples concern different targets or outcomes" in final
    assert "Check prose comparisons against the evidence anchors and supplied passages" in final


def test_source_bundle_preserves_comparative_referents_and_direction() -> None:
    final = _source_bundle_prompt(
        "Sanctions affected both groups, but such action was less frequent for group B.",
        {},
        None,
    ).split("FINAL WHOLE-SOURCE CHECK:")[1]

    assert "resolve source pronouns and contrast markers first" in final
    assert "explicitly name the actor, group, outcome, expression, or measure" in final
    assert "do not use former or latter" in final
    assert "never reverse who or what is higher, lower, more, or less frequent" in final


def test_source_bundle_emits_evidence_before_analysis() -> None:
    prompt = _source_bundle_system_prompt()

    assert "Emit evidence_anchors first" in prompt
    assert "carry their attribution and scope into the later analysis_sections" in prompt


def test_source_bundle_keeps_criticism_attributed_to_its_reporting_work() -> None:
    final = _source_bundle_prompt(
        "A Delta Press report is criticized in a separate commentary.", {}, None,
    ).split("FINAL WHOLE-SOURCE CHECK:")[1]

    assert "Do not credit a discussed work or its publisher with third-party commentary" in final
    assert "Keep that commentary as a separate work" in final
    assert "author and title empty when not supplied" in final


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
        "chunk_prompt_v15": digest(_chunk_system_prompt()),
        "source_bundle_prompt_v32": digest(_source_bundle_system_prompt()),
        "source_bundle_user_prompt_v32": digest(
            _source_bundle_prompt("A fictional source.", {}, None)
        ),
        "codex_source_bundle_schema": bundle_identity["schema_hash"],
        "codex_source_bundle_contract": digest(bundle_identity),
        "codex_chunk_evidence_schema": chunk_identity["schema_hash"],
        "codex_chunk_evidence_contract": digest(chunk_identity),
    } == {
        "atomic_prompt_v14": "8db9f2990d175816cb0100b92d84734ae7c2f930aade66825ed0412b22da3705",
        "chunk_prompt_v15": "44bcaf5bd94ee37686021d925b64a2d3ed0bd5d82b89610c22dd5e0b2e810e20",
        "source_bundle_prompt_v32": "9d6c44822ad4e152e092a602c9c92a73ce5d72532b3b2e99eba80c7f9ad0c21b",
        "source_bundle_user_prompt_v32": "56c574d24421bebf647ad76933ae60574f64aeed5c7609e2e2b0ca2f37db6070",
        "codex_source_bundle_schema": "1b9491a9f2d7bf9c4c8c62a5838180c2e8b171700211e2fe63cd77514d7192c2",
        "codex_source_bundle_contract": "e6e7dc65d7953e5fe777faf1dcb43cebf933c4d9e73ca890264e98a7cd08e023",
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

    assert "cluster synthesis prompt v40" in prompt
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
    assert _synthesis_stage_prompt_version("cluster_synthesis") == "40"
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
