from __future__ import annotations

import json

import pytest

from auto_zettelkasten.fidelity import (
    analyze_atomic_fidelity,
    apply_atomic_replacements,
    source_passages_for_risks,
    validate_atomic_replacements,
)
from auto_zettelkasten.readers import OllamaReader, ProviderError


def test_analyzer_flags_missing_and_mismatched_source_locators() -> None:
    analysis = _analysis(
        detailed_findings=(
            "The report lists 37 wars in Table 2.1 on PDF page 1. "
            "It attributes the framework to Figure 9 on PDF page 8. "
            'It directs readers to heading "Conclusion".'
        ),
        locators=(
            "Table 2.1, PDF page 1; Figure 9, PDF page 8; "
            'heading "Conclusion".'
        ),
    )
    source = _pages(
        {
            1: "Introductory material about settlement durability.",
            2: "Table 2.1: War outcomes. The report lists 37 wars and their outcomes.",
            3: "Discussion of implementation limits.",
        }
    )
    metrics = {
        "page_count": 3,
        "unresolved_pages": [],
        "table_spans": [
            {
                "label": "Table 2.1: War outcomes",
                "page_ordinal": 2,
                "printed_page": "1",
            }
        ],
        "figure_spans": [],
        "heading_spans": [
            {"label": "Introduction", "page_ordinal": 1, "printed_page": "i"}
        ],
    }

    risks = analyze_atomic_fidelity(analysis, source, metrics)
    kinds = {row["kind"] for row in risks}

    assert "locator_page_mismatch" in kinds
    assert "nonexistent_figure_locator" in kinds
    assert "nonexistent_page_locator" in kinds
    assert "nonexistent_heading_locator" in kinds


def test_analyzer_uses_numeric_and_key_tokens_for_adjacent_page_hint() -> None:
    analysis = _analysis(
        detailed_findings=(
            "The source reports 15 negotiated settlements and 7 military victories "
            "among 37 wars (PDF page 1)."
        )
    )
    source = _pages(
        {
            1: "The chapter introduces civil-war settlement research and its cases.",
            2: (
                "The source reports 15 negotiated settlements, 15 ceasefires, and "
                "7 military victories among 37 wars."
            ),
            3: "The next section discusses implementation.",
        }
    )

    risks = analyze_atomic_fidelity(
        analysis,
        source,
        {"page_count": 3, "unresolved_pages": []},
    )
    risk = next(row for row in risks if row["kind"] == "low_page_support")

    assert risk["details"]["cited_page"] == 1
    assert risk["details"]["suggested_page"] == 2
    assert risk["details"]["suggested_page_score"] > risk["details"]["cited_page_score"]


def test_analyzer_resolves_printed_page_labels_to_pdf_ordinals() -> None:
    analysis = _analysis(
        detailed_findings="The result appears on p. 118.",
    )
    source = _pages({1: "Front matter.", 2: "The result appears here."})

    risks = analyze_atomic_fidelity(
        analysis,
        source,
        {
            "page_count": 2,
            "unresolved_pages": [],
            "ordinal_to_printed_page": {"1": "117", "2": "118"},
        },
    )

    assert not [row for row in risks if row["kind"] == "nonexistent_page_locator"]


def test_analyzer_normalizes_bracketed_printed_page_labels() -> None:
    analysis = _analysis(locators="Printed page 71.")
    source = _pages({1: "The article begins on printed page 71."})

    risks = analyze_atomic_fidelity(
        analysis,
        source,
        {
            "page_count": 1,
            "unresolved_pages": [],
            "ordinal_to_printed_page": {"1": "[71]"},
        },
    )

    assert not [row for row in risks if row["kind"] == "nonexistent_page_locator"]


def test_page_locator_requires_separator_and_accepts_bracketed_labels() -> None:
    analysis = _analysis(
        detailed_findings="The P5 members endorsed the finding on p. [663]."
    )

    risks = analyze_atomic_fidelity(
        analysis,
        _pages({1: "663\nThe P5 members endorsed the finding."}),
        {"page_count": 1, "ordinal_to_printed_page": {"1": "[663]"}},
    )

    assert not [row for row in risks if row["kind"] == "nonexistent_page_locator"]


def test_analyzer_infers_consistent_printed_page_offset_from_page_footers() -> None:
    source = _pages(
        {
            1: "Article title\nJOURNAL NAME 95",
            2: "Finding one.\n96 AUTHOR",
            3: "Finding two.\nJOURNAL NAME 97",
            4: "Table 2 reports the result.\n98 AUTHOR",
        }
    )
    risks = analyze_atomic_fidelity(
        _analysis(detailed_findings="Table 2 reports the result on p. 98."),
        source,
        {
            "page_count": 4,
            "ordinal_to_printed_page": {
                "1": "1",
                "2": "2",
                "3": "3",
                "4": "4",
            },
            "table_spans": [
                {"label": "Table 2", "page_ordinal": 4, "printed_page": "98"}
            ],
        },
    )

    assert not [
        row
        for row in risks
        if row["kind"] in {"nonexistent_page_locator", "locator_page_mismatch"}
    ]


def test_analyzer_preserves_dominant_zero_page_offset() -> None:
    source = _pages(
        {
            1: "Finding one.\n1",
            2: "Finding two.\n2",
            3: "Finding three.\n3",
        }
    )

    risks = analyze_atomic_fidelity(
        _analysis(detailed_findings="Finding two appears on p. 2."),
        source,
        {"page_count": 3, "ordinal_to_printed_page": {"1": "2", "2": "3", "3": "4"}},
    )

    assert not [row for row in risks if row["kind"] == "low_page_support"]


def test_object_locator_ignores_prose_and_matches_roman_numeric_aliases() -> None:
    risks = analyze_atomic_fidelity(
        _analysis(
            detailed_findings=(
                "The table of contents is concise, and the agreement figure is stable. "
                "The estimate appears in Table 1 on PDF page 1."
            )
        ),
        _pages({1: "Table I. Estimated models."}),
        {
            "page_count": 1,
            "table_spans": [{"label": "Table I", "page_ordinal": 1}],
        },
    )

    assert not [
        row
        for row in risks
        if row["kind"] in {"nonexistent_table_locator", "nonexistent_figure_locator"}
    ]


def test_analyzer_flags_number_absent_from_cited_and_adjacent_pages() -> None:
    risks = analyze_atomic_fidelity(
        _analysis(detailed_findings="The report identifies 97 commanders (PDF page 2)."),
        _pages(
            {
                1: "Background.",
                2: "The report discusses commanders.",
                3: "Limitations.",
            }
        ),
        {"page_count": 3},
    )

    assert [
        row for row in risks if row["kind"] == "numeric_not_found_near_locator"
    ]


def test_numeric_tokens_normalize_signed_leading_zero() -> None:
    risks = analyze_atomic_fidelity(
        _analysis(detailed_findings="The coefficient was +0.0199 (PDF page 1)."),
        _pages({1: "The estimated coefficient is +.0199."}),
        {"page_count": 1},
    )

    assert not [
        row for row in risks if row["kind"] == "numeric_not_found_near_locator"
    ]


def test_citation_index_note_numbers_are_not_treated_as_findings() -> None:
    risks = analyze_atomic_fidelity(
        _analysis(locators="References appear on page 5 (notes 17–22)."),
        _pages({5: "References appear in footnotes."}),
        {"page_count": 5},
    )

    assert not [
        row
        for row in risks
        if row["kind"] in {"numeric_not_found_near_locator", "low_page_support"}
    ]


def test_source_passages_are_bounded_to_risk_pages_and_neighbors() -> None:
    source = _pages({page: f"Evidence from page {page}." for page in range(1, 8)})
    passages = source_passages_for_risks(
        source,
        [{"claim": "The estimate is reported on PDF page 4.", "details": {}}],
    )

    assert [row["locator"] for row in passages] == [
        "PDF page 3",
        "PDF page 4",
        "PDF page 5",
    ]


def test_source_passages_resolve_printed_page_labels() -> None:
    source = _pages({1: "Front matter.", 2: "Printed page 118 evidence."})
    passages = source_passages_for_risks(
        source,
        [{"claim": "The result appears on p. 118.", "details": {}}],
        page_map={"1": "117", "2": "118"},
    )

    assert [row["locator"] for row in passages] == [
        "PDF page 1",
        "PDF page 2",
    ]


def test_source_passages_include_global_best_page_for_locator_mismatch() -> None:
    passages = source_passages_for_risks(
        _pages(
            {
                1: "Introduction.",
                2: "Unrelated discussion.",
                3: "Conclusion.",
                4: "There were 40 conflicts, an increase of 18 percent.",
                5: "Appendix.",
                6: "References.",
                7: "Other material.",
            }
        ),
        [
            {
                "kind": "numeric_not_found_near_locator",
                "claim": "There were 40 conflicts, an increase of 18 percent (page 7).",
                "details": {},
            }
        ],
    )

    assert "PDF page 4" in {row["locator"] for row in passages}


def test_analyzer_flags_unqualified_causal_upgrade_for_observational_method() -> None:
    analysis = _analysis(
        method_and_research_design=(
            "The study uses an observational cross-national regression and describes "
            "the estimates as correlational."
        ),
        detailed_findings=(
            "Including women in negotiations makes agreements more durable."
        ),
        plain_english_interpretation=(
            "The authors suggest that inclusion may be associated with durability."
        ),
    )

    risks = analyze_atomic_fidelity(analysis, "", {})

    assert [
        row
        for row in risks
        if row["kind"] == "unqualified_causal_wording"
        and row["section_key"] == "detailed_findings"
    ]
    assert not [
        row
        for row in risks
        if row["kind"] == "unqualified_causal_wording"
        and row["section_key"] == "plain_english_interpretation"
    ]


def test_analyzer_does_not_flag_causal_wording_for_identified_design() -> None:
    analysis = _analysis(
        method_and_research_design=(
            "The randomized controlled trial estimates an identified causal effect."
        ),
        detailed_findings="The intervention increased participation.",
    )

    assert not [
        row
        for row in analyze_atomic_fidelity(analysis, "", {})
        if row["kind"] == "unqualified_causal_wording"
    ]


def test_analyzer_does_not_treat_causes_of_conflict_as_a_causal_verb() -> None:
    analysis = _analysis(
        method_and_research_design="Normative practitioner guidance.",
        plain_english_interpretation=(
            "A peace agreement should address the causes of conflict."
        ),
    )

    assert not [
        row
        for row in analyze_atomic_fidelity(analysis, "", {})
        if row["kind"] == "unqualified_causal_wording"
    ]


@pytest.mark.parametrize(
    "claim",
    [
        "The volume finds that engagement is often necessary to increase the chance of settlement.",
        "The guidance identifies principles mediators should follow to increase the chances of success.",
        "The study develops strategies to reduce terrorism risks.",
        "The document describes steps for identifying causes and triggers.",
    ],
)
def test_analyzer_accepts_attributed_or_normative_causal_language(claim: str) -> None:
    risks = analyze_atomic_fidelity(
        _analysis(
            method_and_research_design="Practitioner synthesis.",
            detailed_findings=claim,
        ),
        "",
        {},
    )

    assert not [row for row in risks if row["kind"] == "unqualified_causal_wording"]


@pytest.mark.parametrize(
    "claim",
    [
        "According to the article, external support prevented a perceived stalemate.",
        "The overall association was a reduction in relapse risk.",
        "A one-unit increase was associated with a 95.6% reduction in relapse risk.",
        "The hazard ratio indicates an increased relapse risk.",
    ],
)
def test_analyzer_accepts_explicit_attribution_and_association_language(
    claim: str,
) -> None:
    risks = analyze_atomic_fidelity(
        _analysis(
            method_and_research_design="Observational cross-national regression.",
            detailed_findings=claim,
        ),
        "",
        {},
    )

    assert not [row for row in risks if row["kind"] == "unqualified_causal_wording"]


@pytest.mark.parametrize(
    "claim",
    [
        "It recommends measures that reduce escalation.",
        "The Commission identifies practices that prevent conflict.",
        "Galtung argues that dialogue reduces polarization.",
        "According to Uppsala, conflict has increased since 2013.",
        "As argued by Galtung, structural inequality can produce harm.",
        "Panelists emphasized that inclusion has been shown to reduce violence.",
        "The reported increase in participation followed mediation.",
        "Funding has increased between 2010 and 2020.",
        "Participation increased by 400% after the agreement.",
        "The package included increased financial support.",
        "The outcome was partly attributed to outside support.",
        "The intervention did not lead to durable peace.",
        "Safeguards are needed to prevent escalation.",
    ],
)
def test_analyzer_accepts_extended_attribution_and_descriptive_language(
    claim: str,
) -> None:
    risks = analyze_atomic_fidelity(
        _analysis(
            method_and_research_design="Descriptive practitioner evidence.",
            detailed_findings=claim,
        ),
        "",
        {},
    )

    assert not [row for row in risks if row["kind"] == "unqualified_causal_wording"]


@pytest.mark.parametrize(
    "claim",
    [
        "Inclusion increases durability.",
        "The intervention increased the chance of settlement.",
        "According to the analysis, longer wars increased the chance of mediation.",
    ],
)
def test_analyzer_retains_active_causal_increase_controls(claim: str) -> None:
    risks = analyze_atomic_fidelity(
        _analysis(
            method_and_research_design="Observational regression.",
            detailed_findings=claim,
        ),
        "",
        {},
    )

    assert [row for row in risks if row["kind"] == "unqualified_causal_wording"]


def test_section_locator_parser_does_not_capture_following_page_abbreviation() -> None:
    risks = analyze_atomic_fidelity(
        _analysis(locators="Section 3.3, p. 14."),
        "--- Page 1 ---\n3.3. Findings\nEvidence.",
        {
            "page_count": 1,
            "heading_spans": [{"label": "3.3. Findings", "page_ordinal": 1}],
            "ordinal_to_printed_page": {"1": "14"},
        },
    )

    assert not [row for row in risks if row["kind"] == "nonexistent_heading_locator"]


def test_exact_once_replacement_validation_and_application() -> None:
    original = "Table 2.1 is located on PDF page 13."
    analysis = _analysis(detailed_findings=original)
    payload = {
        "replacements": [
            {
                "section_key": "detailed_findings",
                "original": original,
                "replacement": "Table 2.1 is located on PDF page 14.",
                "evidence_locator": "Table 2.1, PDF page 14",
                "risk_ids": ["atomic-risk-1"],
            }
        ]
    }

    validated = validate_atomic_replacements(
        analysis,
        payload,
        allowed_risk_ids=["atomic-risk-1"],
    )
    updated = apply_atomic_replacements(
        analysis,
        validated,
        allowed_risk_ids=["atomic-risk-1"],
    )

    assert updated["detailed_findings"] == (
        "Table 2.1 is located on PDF page 14."
    )
    assert updated["method_and_research_design"] == analysis[
        "method_and_research_design"
    ]


@pytest.mark.parametrize(
    "mutator",
    [
        lambda payload: {**payload, "analysis": {}},
        lambda payload: {
            "replacements": [
                {
                    **payload["replacements"][0],
                    "section_key": "unknown_section",
                }
            ]
        },
        lambda payload: {
            "replacements": [
                {
                    **payload["replacements"][0],
                    "replacement": "The estimate was 999.",
                }
            ]
        },
        lambda payload: {
            "replacements": [
                {
                    **payload["replacements"][0],
                    "risk_ids": ["unknown-risk"],
                }
            ]
        },
    ],
)
def test_replacement_validator_rejects_unowned_or_unsupported_edits(mutator) -> None:
    original = "The estimate was 10."
    analysis = _analysis(detailed_findings=original)
    payload = {
        "replacements": [
            {
                "section_key": "detailed_findings",
                "original": original,
                "replacement": "The estimate was 11.",
                "evidence_locator": "PDF page 11",
                "risk_ids": ["atomic-risk-1"],
            }
        ]
    }

    with pytest.raises(ValueError):
        validate_atomic_replacements(
            analysis,
            mutator(payload),
            allowed_risk_ids=["atomic-risk-1"],
        )


def test_replacement_validator_rejects_non_unique_original() -> None:
    repeated = "The association is descriptive."
    analysis = _analysis(
        detailed_findings=f"{repeated} Another sentence. {repeated}"
    )

    with pytest.raises(ValueError, match="exactly once"):
        validate_atomic_replacements(
            analysis,
            {
                "replacements": [
                    {
                        "section_key": "detailed_findings",
                        "original": repeated,
                        "replacement": "The reported association is descriptive.",
                        "evidence_locator": "PDF page 2",
                        "risk_ids": ["atomic-risk-1"],
                    }
                ]
            },
            allowed_risk_ids=["atomic-risk-1"],
        )


def test_replacement_validator_can_discard_only_invalid_rows() -> None:
    analysis = _analysis(
        detailed_findings="The estimate was 10. The association was causal."
    )
    valid = {
        "section_key": "detailed_findings",
        "original": "The association was causal.",
        "replacement": "The study reports an association.",
        "evidence_locator": "PDF page 2",
        "risk_ids": ["atomic-risk-1"],
    }

    assert validate_atomic_replacements(
        analysis,
        {
            "replacements": [
                {**valid, "risk_ids": ["unknown-risk"]},
                valid,
            ]
        },
        allowed_risk_ids=["atomic-risk-1"],
        discard_invalid=True,
    ) == [valid]


def test_builtin_verifier_returns_only_validated_replacements() -> None:
    original = "Including women makes agreements more durable."
    response = {
        "replacements": [
            {
                "section_key": "detailed_findings",
                "original": original,
                "replacement": (
                    "The report describes an association between women's inclusion "
                    "and agreement durability."
                ),
                "evidence_locator": "PDF page 14",
                "risk_ids": ["atomic-risk-causal"],
            }
        ]
    }
    reader = _FakeVerifier(json.dumps(response))
    analysis = _analysis(detailed_findings=original)

    result = reader.verify_atomic_claims(
        analysis,
        context={
            "risks": [
                {
                    "risk_id": "atomic-risk-causal",
                    "kind": "unqualified_causal_wording",
                    "section_key": "detailed_findings",
                    "claim": original,
                }
            ],
            "source_passages": [
                {
                    "locator": "PDF page 14",
                    "text": "Evidence is limited and correlational.",
                }
            ],
        },
    )

    assert result == response
    assert "Return exactly one JSON object containing only a replacements array" in (
        reader.system_prompt
    )
    prompt = json.loads(reader.user_prompt)
    assert prompt["risks"][0]["risk_id"] == "atomic-risk-causal"
    assert prompt["source_passages"][0]["locator"] == "PDF page 14"


def test_builtin_verifier_rejects_whole_analysis_response() -> None:
    reader = _FakeVerifier(
        json.dumps({"replacements": [], "analysis": {"thesis": "rewritten"}})
    )

    with pytest.raises(ProviderError, match="contain only a replacements list"):
        reader.verify_atomic_claims(
            _analysis(),
            context={
                "risks": [
                    {
                        "risk_id": "atomic-risk-1",
                        "kind": "low_page_support",
                        "section_key": "detailed_findings",
                        "claim": "A claim.",
                    }
                ]
            },
        )


class _FakeVerifier(OllamaReader):
    def __init__(self, response: str) -> None:
        super().__init__(model="fake", context_window_tokens=128_000)
        self.response = response
        self.system_prompt = ""
        self.user_prompt = ""

    def _generate_text(
        self,
        system_prompt: str,
        user_prompt: str,
        output_tokens: int,
        deadline_seconds: float,
    ) -> str:
        assert output_tokens == 8_000
        assert deadline_seconds > 0
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        return self.response


def _analysis(**updates: str) -> dict[str, str]:
    analysis = {
        "thesis": "The source examines settlement durability.",
        "method_and_research_design": "The source provides descriptive evidence.",
        "evidence_and_data": "The source uses a collection of conflict cases.",
        "detailed_findings": "The source reports bounded descriptive findings.",
        "plain_english_interpretation": "The reported pattern is descriptive.",
        "strengths_and_contributions": "The source organizes comparable cases.",
        "methodological_critique": "The design does not identify causal effects.",
        "limitations": "The comparison is observational.",
        "what_this_source_can_support": "The source supports descriptive claims.",
        "what_this_source_cannot_support": "It cannot establish causation.",
        "locators": "PDF page 1.",
    }
    analysis.update(updates)
    return analysis


def _pages(values: dict[int, str]) -> str:
    return "\n\n".join(
        f"--- Page {page} ---\n{text}"
        for page, text in sorted(values.items())
    )
