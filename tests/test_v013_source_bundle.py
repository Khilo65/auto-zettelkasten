from __future__ import annotations

from copy import deepcopy
import json
import threading

import pytest

from auto_zettelkasten.models import (
    EvidenceProfile,
    MapRequest,
    MissingSourceRecommendation,
    ProcessingPolicy,
    SourceAnalysisBundle,
)
from auto_zettelkasten.api import resume_map, run_map
from auto_zettelkasten.files import read_yaml, write_yaml
from auto_zettelkasten.notes import read_note
from auto_zettelkasten.pipeline import (
    SourceBundleQuantitativeProvenanceError,
    _ProfileProviderBudget,
    _commit_literature_memory,
    _commit_remediation_ledgers,
    _literature_position_relations,
    _match_literature_position,
    _match_literature_position_detail,
    _read_document,
    _recover_saved_source_bundle,
    _reusable_note,
    _source_bundle_from_result,
)
from auto_zettelkasten.relationships import stable_hash
from auto_zettelkasten.readers import (
    DeepSeekReader,
    ProviderError,
    _parse_source_bundle_response,
)

from conftest import FakeZotero


def test_resolved_literature_position_projects_cites_and_cited_by(
    tmp_path,
) -> None:
    write_yaml(
        tmp_path
        / "02_source_memory"
        / "indexes"
        / "literature_positions.yml",
        {
            "positions": [
                {
                    "literature_position_id": "position-a-b",
                    "current_source_id": "source-a",
                    "matched_source_id": "source-b",
                    "engagement": "A uses B's result.",
                    "locator": "p. 4",
                }
            ]
        },
    )
    relations = _literature_position_relations(
        tmp_path,
        [
            EvidenceProfile(source_id="source-a", note_id="note-a"),
            EvidenceProfile(source_id="source-b", note_id="note-b"),
        ],
    )

    assert [(row["source_id"], row["target_source_id"], row["relation_type"]) for row in relations] == [
        ("source-a", "source-b", "cites"),
        ("source-b", "source-a", "cited_by"),
    ]


def _bundle_payload() -> dict:
    return {
        "bundle_schema_version": "1",
        "source_identity": {
            "source_id": "source-zotero-A1",
            "zotero_key": "A1",
        },
        "observed_bibliographic_identity": {"title": "Observed title"},
        "scope_assessment": {
            "source_scope": "partial_document",
            "evidence_eligibility": "substantive_bounded",
        },
        "analysis_sections": {
            "thesis": "The author argues that monitoring changes implementation.",
            "method_and_research_design": "Comparative qualitative analysis.",
        },
        "compact_profile": {
            "thesis": "Monitoring changes implementation.",
            "method_or_knowledge_basis": "Comparative qualitative analysis.",
            "source_genre": "journal article",
            "inferential_design": "comparative observational",
            "coverage": {"status": "partial"},
            "concepts": ["credible commitment"],
            "theories": ["commitment problem"],
            "mechanisms": ["monitoring"],
            "outcomes": ["implementation"],
            "cases": ["civil wars"],
            "populations": ["peace agreements"],
            "periods": ["post-conflict"],
            "datasets": ["agreement dataset"],
            "measures": ["implementation rate"],
        },
        "evidence_anchors": [
            {
                "evidence_anchor_id": "anchor-1",
                "source_id": "source-zotero-A1",
                "claim": "Monitoring changes implementation.",
                "locator": "p. 12",
                "planning_roles": ["thesis", "major_finding"],
                "salience_priority": 10,
                "support_boundary": "Recovered pages 1-20 only.",
            }
        ],
        "literature_positions": [
            {
                "current_source_id": "source-zotero-A1",
                "raw_citation": "Walter 1997",
                "author": "Walter",
                "year": "1997",
                "title": "The Critical Barrier to Civil War Settlement",
                "identifiers": {},
                "engagement": "Builds on the commitment-problem account.",
                "relation_label": "builds_on",
                "locator": "p. 4",
                "matched_source_id": "",
                "provenance": "explicit",
            }
        ],
        "missing_source_recommendations": [],
        "self_review": {"passed": True},
    }


def test_bundle_is_source_owned_and_optional_rows_are_isolated() -> None:
    payload = _bundle_payload()
    payload["literature_positions"].append({"broken": True})

    bundle = SourceAnalysisBundle.from_dict(payload)

    assert bundle.source_identity["source_id"] == "source-zotero-A1"
    assert len(bundle.literature_positions) == 1
    assert bundle.component_diagnostics[0]["component"] == "literature_positions"
    assert bundle.evidence_anchors[0].planning_roles == [
        "thesis",
        "major_finding",
    ]


def test_source_bundle_envelope_recovery_is_unambiguous_and_source_owned() -> None:
    payload = _bundle_payload()
    expected = {"source_id": "source-zotero-A1", "zotero_key": "A1"}

    assert _parse_source_bundle_response(
        [payload], label="bundle", expected_identity=expected
    )["source_identity"]["source_id"] == "source-zotero-A1"
    assert _parse_source_bundle_response(
        f'preface {{"noise": true}} middle {json.dumps(payload)} epilogue',
        label="bundle",
        expected_identity=expected,
    )["source_identity"]["source_id"] == "source-zotero-A1"

    conflicting = deepcopy(payload)
    conflicting["analysis_sections"]["thesis"] = "A different source interpretation."
    with pytest.raises(ProviderError, match="multiple valid"):
        _parse_source_bundle_response(
            f"{json.dumps(payload)}\n{json.dumps(conflicting)}",
            label="bundle",
            expected_identity=expected,
        )
    with pytest.raises(ProviderError, match="no complete source-owned"):
        _parse_source_bundle_response(
            json.dumps(payload).replace(
                "source-zotero-A1", "source-zotero-wrong"
            ),
            label="bundle",
            expected_identity=expected,
        )


def test_source_bundle_conservative_yaml_recovery_uses_local_identity() -> None:
    recovered = _parse_source_bundle_response(
        """
analysis_sections:
  thesis: Monitoring changes implementation.
  method_and_research_design: Comparative qualitative analysis.
  evidence_and_data: 1093 dyad-years.
  detailed_findings: Monitoring is associated with implementation.
compact_profile:
  thesis: Monitoring changes implementation.
  method_or_knowledge_basis: Comparative qualitative analysis.
evidence_anchors:
  - claim: Monitoring is associated with implementation.
    locator: p. 12
    planning_roles: major_finding
    salience_priority: 10
    evidence_role: associational
    support_boundary: Recovered text only.
literature_positions:
  - raw_citation: Walter 1997
    author: Walter
    year: 1997
    title: The Critical Barrier to Civil War Settlement
    identifiers: {}
    engagement: Builds on the commitment-problem account.
    relation_label: builds_on
    locator: p. 4
observed_bibliographic_identity:
  title: Observed title
""",
        label="bundle",
        expected_identity={
            "source_id": "source-zotero-A1",
            "zotero_key": "A1",
        },
    )

    assert recovered["source_identity"] == {
        "source_id": "source-zotero-A1",
        "zotero_key": "A1",
    }
    assert recovered["bundle_schema_version"] == "1"
    assert recovered["evidence_anchors"][0]["source_id"] == "source-zotero-A1"
    assert recovered["evidence_anchors"][0]["evidence_anchor_id"]
    assert recovered["literature_positions"][0]["year"] == "1997"
    assert (
        recovered["literature_positions"][0]["current_source_id"]
        == "source-zotero-A1"
    )


def test_source_bundle_repairs_only_lexical_json_defects() -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["uncertainty"] = (
        'Low; "positive association" but no effect size.'
    )
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "sample": "1093 (same model sample)",
    }
    raw = json.dumps(payload, indent=2)
    raw = raw.replace(
        '\\"positive association\\"',
        '"positive association"',
    ).replace(
        '"sample": "1093 (same model sample)"',
        '"sample": 1093 (same model sample)',
    )

    recovered = _parse_source_bundle_response(
        raw,
        label="bundle",
        expected_identity={
            "source_id": "source-zotero-A1",
            "zotero_key": "A1",
        },
    )

    assert recovered["analysis_sections"]["thesis"]
    assert recovered["evidence_anchors"][0]["quantitative_result"]["sample"] == (
        "1093 (same model sample)"
    )
    assert any(
        row.get("reason") == "conservative_json_lexical_recovery"
        for row in recovered["component_diagnostics"]
    )


def test_equivalent_local_recovery_routes_do_not_create_false_ambiguity(
    monkeypatch,
) -> None:
    import auto_zettelkasten.readers as readers

    first = _bundle_payload()
    second = deepcopy(first)
    second["self_review"] = {"ignored_provider_field": True}
    monkeypatch.setattr(
        readers,
        "_conservative_json_superset_mapping",
        lambda _text: first,
    )
    monkeypatch.setattr(
        readers,
        "_conservative_json_repair_mapping",
        lambda _text: second,
    )

    recovered = _parse_source_bundle_response(
        "{malformed",
        label="bundle",
        expected_identity={
            "source_id": "source-zotero-A1",
            "zotero_key": "A1",
        },
    )

    assert recovered["source_identity"]["source_id"] == "source-zotero-A1"


def test_local_recovery_prefers_the_unique_text_completion(monkeypatch) -> None:
    import auto_zettelkasten.readers as readers

    shorter = _bundle_payload()
    shorter["evidence_anchors"][0]["quantitative_result"] = {
        "sample": "175 (122 ethnic",
    }
    complete = deepcopy(shorter)
    complete["evidence_anchors"][0]["quantitative_result"]["sample"] = (
        "175 (122 ethnic, 53 nonethnic)"
    )
    monkeypatch.setattr(
        readers,
        "_conservative_json_superset_mapping",
        lambda _text: shorter,
    )
    monkeypatch.setattr(
        readers,
        "_conservative_json_repair_mapping",
        lambda _text: complete,
    )

    recovered = _parse_source_bundle_response(
        "{malformed",
        label="bundle",
        expected_identity={
            "source_id": "source-zotero-A1",
            "zotero_key": "A1",
        },
    )

    assert recovered["evidence_anchors"][0]["quantitative_result"]["sample"] == (
        "175 (122 ethnic, 53 nonethnic)"
    )


def test_source_bundle_coerces_optional_evidence_scalars_without_losing_anchor() -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["salience_priority"] = "high"
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "statistic": -2.4,
        "estimate": 0.42,
        "baseline": 0,
        "sample": 1093,
        "provenance": "Reported in the source's regression table.",
        "provider_comment": "not part of the quantitative-result contract",
    }

    recovered = _parse_source_bundle_response(
        payload,
        label="bundle",
        expected_identity={
            "source_id": "source-zotero-A1",
            "zotero_key": "A1",
        },
    )

    anchor = recovered["evidence_anchors"][0]
    assert anchor["salience_priority"] == 10
    assert anchor["quantitative_result"]["statistic"] == "-2.4"
    assert anchor["quantitative_result"]["estimate"] == "0.42"
    assert anchor["quantitative_result"]["baseline"] == "0"
    assert anchor["quantitative_result"]["sample"] == "1093"
    assert anchor["quantitative_result"]["provenance"] == "source_reported"
    assert "provider_comment" not in anchor["quantitative_result"]


def test_saved_source_failure_reparses_locally_without_provider_call(
    tmp_path,
) -> None:
    checkpoint = tmp_path / "items" / "A1"
    checkpoint.mkdir(parents=True)
    raw = """
analysis_sections:
  thesis: Monitoring changes implementation.
  method_and_research_design: Comparative analysis.
  evidence_and_data: 1093 dyad-years.
  detailed_findings: Monitoring is associated with implementation.
compact_profile:
  thesis: Monitoring changes implementation.
  method_or_knowledge_basis: Comparative analysis.
evidence_anchors: []
literature_positions: []
observed_bibliographic_identity: {}
"""
    from auto_zettelkasten.files import write_yaml

    write_yaml(
        checkpoint / "source_failure.yml",
        {
            "source_id": "source-zotero-A1",
            "zotero_item_key": "A1",
            "fingerprint": "fingerprint-a",
            "raw_response": raw,
            "raw_response_hash": "raw-a",
        },
    )

    recovered = _recover_saved_source_bundle(
        checkpoint,
        source_id="source-zotero-A1",
        zotero_key="A1",
        fingerprint="fingerprint-a",
    )

    assert recovered is not None
    assert recovered["analysis_sections"]["thesis"]
    recovery = read_yaml(checkpoint / "source_recovery.yml")
    assert recovery["provider_calls"] == 0


def test_unchanged_source_contract_failure_is_not_retried(tmp_path) -> None:
    class InvalidEnvelopeReader(BundleReader):
        is_cloud = True

        def read_source_bundle(self, text, metadata, question=None):
            del text, metadata, question
            self.calls += 1
            error = ProviderError("invalid source envelope")
            error.raw_response = '{"analysis_sections":'
            raise error

    item = {
        "key": "A1",
        "data": {
            "key": "A1",
            "itemType": "journalArticle",
            "title": "Source A",
            "date": "2024",
            "creators": [{"creatorType": "author", "lastName": "Author"}],
        },
    }
    reader = InvalidEnvelopeReader()
    request = MapRequest(
        tmp_path,
        provider="deepseek",
        model="bundle-v1",
        allow_cloud=True,
    )

    first = run_map(
        request,
        client=FakeZotero([item]),
        reader=reader,
        run_id="terminal-contract",
    )
    resumed = resume_map(
        tmp_path,
        "terminal-contract",
        client=FakeZotero([item]),
        reader=reader,
    )

    assert first.parked_for_review_count == resumed.parked_for_review_count == 1
    assert reader.calls == 1
    assert resumed.items[0]["reason"] == "reader_failed:ProviderError"


@pytest.mark.parametrize(
    "raw",
    [
        "analysis_sections: &sections {thesis: A}\ncopy: *sections\n",
        "analysis_sections: {thesis: A}\n---\nanalysis_sections: {thesis: B}\n",
        "analysis_sections: {thesis: A, thesis: B}\n",
        "!!python/object/apply:os.system ['echo unsafe']\n",
    ],
)
def test_source_bundle_conservative_yaml_recovery_rejects_ambiguous_features(
    raw: str,
) -> None:
    with pytest.raises(ProviderError, match="no complete source-owned"):
        _parse_source_bundle_response(
            raw,
            label="bundle",
            expected_identity={"source_id": "source-zotero-A1"},
        )


def test_missing_source_memory_retains_retrieval_context_and_strong_ids() -> None:
    recommendation = MissingSourceRecommendation(
        raw_citation="Example report",
        identifiers={"isbn": "978-1-4028-9462-6"},
        relevant_collections=["Conflict Relapse"],
        relevant_topics=["credible commitments"],
        relevant_clusters=["implementation mechanisms"],
    )

    assert recommendation.relevant_collections == ["Conflict Relapse"]
    assert (
        _match_literature_position(
            recommendation.to_dict(),
            {
                "by_doi": {},
                "by_isbn": {"9781402894626": "source-book"},
                "by_url": {},
                "by_identity": {},
                "by_source_id": {},
            },
        )
        == "source-book"
    )


def test_berg_style_citations_match_compatible_first_author_surnames() -> None:
    index = {
        "by_source_id": {
            "source-walter": {
                "source_id": "source-walter",
                "zotero_key": "WALTER",
                "title": "committing to peace the successful settlement of civil wars",
                "author": "walter",
                "author_surnames": ["walter"],
                "year": "2002",
            },
            "source-hegre": {
                "source_id": "source-hegre",
                "zotero_key": "HEGRE",
                "title": "governance and conflict relapse",
                "author": "hegre",
                "author_surnames": ["hegre", "nygard"],
                "year": "2015",
            },
        },
        "by_zotero_key": {},
        "by_doi": {},
        "by_isbn": {},
        "by_url": {},
        "known_zotero_items": [],
    }

    walter = _match_literature_position_detail(
        {
            "title": "Committing to Peace: The Successful Settlement of Civil Wars",
            "author": "Walter, Barbara F.",
            "year": "2002",
        },
        index,
    )
    hegre = _match_literature_position_detail(
        {
            "title": "Governance and Conflict Relapse",
            "author": "Håvard Hegre",
            "year": "2015",
        },
        index,
    )

    assert walter["source_id"] == "source-walter"
    assert hegre["source_id"] == "source-hegre"
    assert walter["basis"] == hegre["basis"] == "title_year_first_author"


@pytest.mark.parametrize("candidate_year", ["", "2001"])
@pytest.mark.parametrize(
    ("citation_title", "candidate_title"),
    [
        ("A Theory of Durable Peace Settlements", "a theory of durable peace settlements"),
        ("Theory of Durable Peace Settlements", "a theory of durable peace settlements"),
    ],
)
def test_title_literature_match_requires_confirmed_citation_year(
    candidate_year: str,
    citation_title: str,
    candidate_title: str,
) -> None:
    match = _match_literature_position_detail(
        {
            "title": citation_title,
            "author": "Scholar",
            "year": "2002",
        },
        {
            "by_source_id": {
                "source-candidate": {
                    "source_id": "source-candidate",
                    "zotero_key": "CANDIDATE",
                    "title": candidate_title,
                    "author_surnames": ["scholar"],
                    "year": candidate_year,
                }
            },
            "by_zotero_key": {},
            "by_doi": {},
            "by_isbn": {},
            "by_url": {},
            "known_zotero_items": [],
        },
    )

    assert match["status"] == "not_in_snapshot"
    assert match["source_id"] == ""


def test_title_literature_match_without_citation_year_remains_available() -> None:
    match = _match_literature_position_detail(
        {"title": "A Theory of Durable Peace Settlements", "author": "Scholar"},
        {
            "by_source_id": {
                "source-candidate": {
                    "source_id": "source-candidate",
                    "zotero_key": "CANDIDATE",
                    "title": "a theory of durable peace settlements",
                    "author_surnames": ["scholar"],
                    "year": "",
                }
            },
            "by_zotero_key": {},
            "by_doi": {},
            "by_isbn": {},
            "by_url": {},
            "known_zotero_items": [],
        },
    )

    assert match["source_id"] == "source-candidate"
    assert match["basis"] == "title_year_first_author"


@pytest.mark.parametrize("candidate_year", ["", "2001"])
def test_known_unmapped_title_match_requires_confirmed_citation_year(
    candidate_year: str,
) -> None:
    match = _match_literature_position_detail(
        {
            "title": "A Theory of Durable Peace Settlements",
            "author": "Scholar",
            "year": "2002",
        },
        {
            "by_source_id": {},
            "by_zotero_key": {},
            "by_doi": {},
            "by_isbn": {},
            "by_url": {},
            "known_zotero_items": [
                {
                    "zotero_key": "KNOWN",
                    "title": "a theory of durable peace settlements",
                    "author_surnames": ["scholar"],
                    "year": candidate_year,
                    "doi": "",
                    "isbn": "",
                    "url": "",
                }
            ],
        },
    )

    assert match["status"] == "not_in_snapshot"
    assert match["zotero_key"] == ""


def test_literature_match_distinguishes_known_unmapped_from_absent() -> None:
    index = {
        "by_source_id": {},
        "by_zotero_key": {},
        "by_doi": {},
        "by_isbn": {},
        "by_url": {},
        "known_zotero_items": [
            {
                "zotero_key": "KNOWN",
                "title": "known report",
                "author_surnames": ["author"],
                "year": "2020",
                "doi": "10.1000/known",
                "isbn": "",
                "url": "",
            }
        ],
    }

    known = _match_literature_position_detail(
        {
            "title": "Known Report",
            "author": "Author",
            "year": "2020",
            "identifiers": {"doi": "10.1000/known"},
        },
        index,
    )
    absent = _match_literature_position_detail(
        {
            "title": "Absent Report",
            "author": "Other",
            "year": "2021",
        },
        index,
    )

    assert known["status"] == "known_zotero_unmapped"
    assert known["zotero_key"] == "KNOWN"
    assert absent["status"] == "not_in_snapshot"


def test_literature_match_normalizes_doi_urls() -> None:
    match = _match_literature_position_detail(
        {"identifiers": {"doi": "https://doi.org/10.1000/KNOWN"}},
        {
            "by_source_id": {
                "source-known": {
                    "source_id": "source-known",
                    "zotero_key": "KNOWN",
                }
            },
            "by_zotero_key": {},
            "by_doi": {"10.1000/known": ["source-known"]},
            "by_isbn": {},
            "by_url": {},
            "known_zotero_items": [],
        },
    )

    assert match["source_id"] == "source-known"
    assert match["basis"] == "doi"


def test_literature_match_rejects_container_doi_with_different_work_title() -> None:
    match = _match_literature_position_detail(
        {
            "title": "Chapter Two",
            "author": "Author",
            "year": "2020",
            "identifiers": {"doi": "10.4324/9781003048404"},
        },
        {
            "by_source_id": {
                "source-chapter-one": {
                    "source_id": "source-chapter-one",
                    "zotero_key": "CHAPTER1",
                    "title": "chapter one",
                    "author": "author",
                    "author_surnames": ["author"],
                    "year": "2020",
                    "item_type": "booksection",
                }
            },
            "by_zotero_key": {},
            "by_doi": {"10.4324/9781003048404": ["source-chapter-one"]},
            "by_isbn": {},
            "by_url": {},
            "known_zotero_items": [],
        },
    )

    assert match["status"] == "not_in_snapshot"


def test_remediation_ledgers_record_creator_and_scope_discrepancies(
    tmp_path,
) -> None:
    payload = _bundle_payload()
    payload["observed_bibliographic_identity"] = {
        "title": "Canonical title",
        "creators": [{"creatorType": "author", "lastName": "Observed"}],
    }
    payload["scope_assessment"] = {
        "source_scope": "partial_document",
        "evidence_eligibility": "substantive_bounded",
    }
    row = {
        "source_id": "source-zotero-a1",
        "zotero_item_key": "A1",
        "source_scope": "full_document",
        "evidence_eligibility": "substantive_full",
        "item": {
            "key": "A1",
            "data": {
                "key": "A1",
                "title": "Canonical title",
                "creators": [
                    {"creatorType": "author", "lastName": "Canonical"}
                ],
                "itemType": "journalArticle",
            },
        },
    }

    _commit_remediation_ledgers(
        tmp_path, row, SourceAnalysisBundle.from_dict(payload)
    )

    metadata = read_yaml(
        tmp_path / "01_custody" / "zotero" / "zotero_metadata_issues.yml"
    )
    classification = read_yaml(
        tmp_path / "11_state" / "pipeline_classification_issues.yml"
    )
    assert "creators" in metadata["issues"][0]["recommended_correction"]
    assert {
        row["field"] for row in classification["issues"][0]["diagnostics"]
    } == {"source_scope", "evidence_eligibility"}


def test_pathways_for_peace_book_metadata_recommends_institutional_report_review(
    tmp_path,
) -> None:
    payload = _bundle_payload()
    payload["observed_bibliographic_identity"] = {
        "title": "Pathways for Peace: Inclusive Approaches to Preventing Violent Conflict",
        "creators": [
            {"creatorType": "author", "name": "United Nations"},
            {"creatorType": "author", "name": "World Bank"},
        ],
        "date": "2018",
        "itemType": "report",
    }
    row = {
        "source_id": "source-zotero-pathways",
        "zotero_item_key": "PATHWAYS",
        "item": {
            "key": "PATHWAYS",
            "data": {
                "key": "PATHWAYS",
                "title": payload["observed_bibliographic_identity"]["title"],
                "creators": [
                    {"creatorType": "editor", "name": "World Bank Group"}
                ],
                "date": "2018",
                "itemType": "book",
            },
        },
    }

    _commit_remediation_ledgers(
        tmp_path, row, SourceAnalysisBundle.from_dict(payload)
    )

    issue = read_yaml(
        tmp_path / "01_custody" / "zotero" / "zotero_metadata_issues.yml"
    )["issues"][0]
    assert "probable_document_type_mismatch" in issue["issue_types"]
    assert "institutional_report_represented_as_book" in issue["issue_types"]
    assert "probable_creator_role_mismatch" in issue["issue_types"]


def test_bundle_repairs_mechanical_envelope_errors_without_losing_analysis() -> None:
    payload = _bundle_payload()
    payload["bundle_schema_version"] = 1
    payload["observed_bibliographic_identity"] = "not reported"
    payload["scope_assessment"] = None
    payload["self_review"] = ["passed"]

    bundle = SourceAnalysisBundle.from_dict(payload)

    assert bundle.bundle_schema_version == "1"
    assert bundle.analysis_sections["thesis"]
    assert bundle.observed_bibliographic_identity == {}
    assert bundle.scope_assessment == {}
    assert bundle.self_review == {}
    assert {
        row["component"] for row in bundle.component_diagnostics
    } == {
        "observed_bibliographic_identity",
        "scope_assessment",
        "self_review",
    }


def test_pipeline_does_not_reinsert_rejected_optional_rows() -> None:
    payload = _bundle_payload()
    payload["component_diagnostics"] = [
        {
            "component": "evidence_anchors",
            "reason": "invalid_optional_row",
            "raw": {
                "source_id": "source-zotero-wrong",
                "claim": "Malformed row must remain diagnostic only.",
            },
        }
    ]

    bundle = _source_bundle_from_result(
        payload,
        {
            "source_id": "source-zotero-A1",
            "zotero_item_key": "A1",
        },
        "full_document",
    )

    assert bundle is not None
    assert [row.claim for row in bundle.evidence_anchors] == [
        "Monitoring changes implementation."
    ]
    assert bundle.component_diagnostics[0]["raw"]["source_id"] == (
        "source-zotero-wrong"
    )


def test_native_html_heading_reaches_cluster_admission_without_weak_locator_promotion() -> None:
    from auto_zettelkasten.extraction import extract_bytes
    from auto_zettelkasten.literature import (
        _anchor_is_synthesis_eligible,
        _global_plan_proposals,
        normalize_evidence_profiles,
    )
    from auto_zettelkasten.pipeline import _source_read_metadata_hash, _source_reader_metadata

    heading = "Specific monitoring mechanisms"
    raw = (
        f'<h4>{heading}</h4><p>Monitoring changes implementation.</p>'
        '<h4>Ambiguous source heading</h4><h4>Ambiguous source heading</h4>'
        '<h4>Detailed Findings</h4><h4>Methods</h4>'
    )
    extracted = extract_bytes(raw.encode(), media_type="text/html", filename="source.html")
    row = {
        "source_id": "source-zotero-A1", "zotero_item_key": "A1",
        "media_type": "text/html", "text": extracted.text,
        "coverage_metrics": extracted.coverage_metrics,
    }
    metadata = _source_reader_metadata({}, row["source_id"], "A1", row)
    assert _source_read_metadata_hash(metadata) != _source_read_metadata_hash(
        _source_reader_metadata({}, row["source_id"], "A1", {**row, "coverage_metrics": {}})
    )
    payload = _bundle_payload()
    anchor = payload["evidence_anchors"][0]
    anchor.update(locator=heading, locators=[heading])
    bundle = _source_bundle_from_result(payload, row, "full_document")
    assert bundle is not None
    assert bundle.evidence_anchors[0].locator == f'Heading "{heading}"'
    assert bundle.evidence_anchors[0].locators == [f'Heading "{heading}"']
    assert bundle.evidence_anchors[0].support_envelope.argument_role == "none"
    replayed = _source_bundle_from_result(bundle.to_dict(), row, "full_document")
    assert replayed is not None and replayed.semantic_dict() == bundle.semantic_dict()
    source_profile = {
        "source_id": row["source_id"], "note_id": "note-a",
        "note_status": "analytical_atomic_note",
        "evidence_eligibility": "substantive_bounded",
        "evidence_anchors": [value.to_dict() for value in bundle.evidence_anchors],
    }
    profiles = normalize_evidence_profiles([source_profile, {
        **source_profile, "source_id": "source-b", "note_id": "note-b",
        "evidence_anchors": [{
            **source_profile["evidence_anchors"][0], "source_id": "source-b",
            "evidence_anchor_id": "anchor-b", "locator": "p. 2", "locators": ["p. 2"],
        }],
    }])
    assert _anchor_is_synthesis_eligible(profiles[0]["claims"][0])
    proposal = {"clusters": [{
        "cluster_id": "native-heading", "shared_question": "How does monitoring work?",
        "members": [{"source_id": profile["source_id"], "role": "core",
                     "evidence_anchor_ids": [profile["claims"][0]["evidence_anchor_id"]]}
                    for profile in profiles],
    }]}
    admitted, _parked, _neighbors, _unclustered = _global_plan_proposals(proposal, profiles)
    assert admitted[0]["source_roles"] == {row["source_id"]: "core", "source-b": "core"}
    for locator in ("Ambiguous source heading", "Unverified source heading", "Detailed Findings", "Methods"):
        anchor.update(locator=locator, locators=[locator])
        unchanged = _source_bundle_from_result(payload, row, "full_document")
        assert unchanged is not None and unchanged.evidence_anchors[0].locator == locator
    anchor.update(locator=heading, locators=[heading])
    legacy = _source_bundle_from_result(payload, {**row, "coverage_metrics": {}}, "full_document")
    assert legacy is not None and legacy.evidence_anchors[0].locator == heading


def test_pipeline_rehydrates_safe_same_source_evidence_diagnostics_idempotently() -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"] = []
    payload["component_diagnostics"] = [
        {
            "component": "evidence_anchors",
            "row_index": 0,
            "reason": "ValueError:quantitative result.estimate must be string",
            "raw": {
                "source_id": "source-zotero-A1",
                "claim": "Monitoring changes implementation.",
                "locator": "p. 12",
                "planning_roles": "major_finding",
                "salience_priority": "critical",
                "quantitative_result": {
                    "estimate": 42,
                    "baseline": None,
                    "provenance": "Reported by the article.",
                    "unknown_provider_field": "ignored",
                },
            },
        }
    ]
    row = {
        "source_id": "source-zotero-A1",
        "zotero_item_key": "A1",
        "text": "Reported estimate: 42.",
    }

    recovered = _source_bundle_from_result(payload, row, "full_document")

    assert recovered is not None
    assert len(recovered.evidence_anchors) == 1
    anchor = recovered.evidence_anchors[0]
    assert anchor.source_id == "source-zotero-A1"
    assert anchor.planning_roles == ["major_finding"]
    assert anchor.salience_priority == 10
    assert anchor.quantitative_result is not None
    assert anchor.quantitative_result.estimate == "42"
    assert anchor.quantitative_result.baseline == ""
    assert anchor.quantitative_result.provenance == "source_reported"

    replayed = _source_bundle_from_result(
        recovered.to_dict(),
        row,
        "full_document",
    )

    assert replayed is not None
    assert replayed.semantic_dict() == recovered.semantic_dict()
    assert len(replayed.evidence_anchors) == 1


def test_bundle_canonicalizes_duplicate_provider_anchor_ids_without_collapsing_claims() -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"].append(
        {
            **payload["evidence_anchors"][0],
            "claim": "A distinct claim at the same source location.",
        }
    )

    bundle = _source_bundle_from_result(
        payload,
        {"source_id": "source-zotero-A1", "zotero_item_key": "A1"},
        "full_document",
    )

    assert bundle is not None
    assert len(bundle.evidence_anchors) == 2
    assert len({row.evidence_anchor_id for row in bundle.evidence_anchors}) == 2


def test_bundle_normalizes_provider_helper_shapes_without_dropping_rows() -> None:
    payload = _bundle_payload()
    payload["analysis_sections"]["thesis"] = {
        "main_claim": "Monitoring changes implementation.",
        "caveat": "The comparison is observational.",
    }
    payload["compact_profile"] = {
        "thesis": "Monitoring changes implementation.",
        "bounded_facets": {
            "mechanisms": ["monitoring"],
            "cases": "civil wars",
        },
    }
    payload["evidence_anchors"][0]["support_envelope"] = (
        "Recovered pages only."
    )
    payload["literature_positions"][0] = {
        "current_source_id": "source-zotero-A1",
        "raw_citation": "Walter 1997",
        "normalized": {
            "author": "Walter",
            "year": "1997",
            "title": "The Critical Barrier to Civil War Settlement",
        },
        "identifiers": {},
        "engagement_account": "Builds on the commitment-problem account.",
        "relation_label": "builds_on",
        "locator": "p. 4",
        "provenance": "explicit",
    }

    bundle = SourceAnalysisBundle.from_dict(payload)

    assert "Main claim: Monitoring changes implementation." in bundle.analysis_sections[
        "thesis"
    ]
    assert bundle.compact_profile["mechanisms"] == ["monitoring"]
    assert bundle.compact_profile["cases"] == ["civil wars"]
    assert bundle.evidence_anchors[0].support_envelope.support_status == "supported"
    assert bundle.literature_positions[0].author == "Walter"
    assert bundle.literature_positions[0].engagement.startswith("Builds on")


def test_literature_match_is_excluded_from_bundle_semantic_identity() -> None:
    first = SourceAnalysisBundle.from_dict(_bundle_payload())
    changed = _bundle_payload()
    changed["literature_positions"][0]["matched_source_id"] = "source-zotero-B2"
    second = SourceAnalysisBundle.from_dict(changed)

    assert first.semantic_dict() == second.semantic_dict()
    assert first.to_dict() != second.to_dict()


def test_profile_v13_serializes_one_evidence_eligibility_field() -> None:
    profile = EvidenceProfile(
        note_id="note-1",
        source_id="source-1",
        evidence_eligibility="context_only",
    )

    payload = profile.to_dict()

    assert payload["profile_schema_version"] == "1.3"
    assert payload["evidence_eligibility"] == "context_only"
    assert "excluded_from_synthesis" not in payload
    assert profile.excluded_from_synthesis is True


class BundleReader:
    name = "bundle-reader"
    model = "bundle-v1"
    is_cloud = False

    def __init__(self) -> None:
        self.calls = 0

    def read_source_bundle(self, text, metadata, question=None):
        del text, question
        self.calls += 1
        payload = _bundle_payload()
        context = metadata["_source_context"]
        payload["source_identity"] = {
            "source_id": context["source_id"],
            "zotero_key": context["zotero_key"],
        }
        payload["evidence_anchors"][0]["source_id"] = context["source_id"]
        payload["literature_positions"][0]["current_source_id"] = context[
            "source_id"
        ]
        payload["analysis_sections"] = {
            key: f"Grounded {key}; see page 1."
            for key in (
                "thesis",
                "method_and_research_design",
                "evidence_and_data",
                "detailed_findings",
                "plain_english_interpretation",
                "strengths_and_contributions",
                "methodological_critique",
                "limitations",
                "what_this_source_can_support",
                "what_this_source_cannot_support",
                "locators",
            )
        }
        return payload

    def read_source(self, *args, **kwargs):
        raise AssertionError("legacy source call must not run")


def test_ordinary_bundle_source_uses_one_call_and_no_profile_or_fidelity_call(
    tmp_path,
) -> None:
    item = {
        "key": "ITEMA",
        "data": {
            "key": "ITEMA",
            "itemType": "journalArticle",
            "title": "Institutions and Reform",
            "date": "2024",
            "creators": [{"creatorType": "author", "lastName": "One"}],
        },
    }
    reader = BundleReader()

    report = run_map(
        MapRequest(tmp_path, provider="ollama", model="bundle-v1", parallel=1),
        client=FakeZotero([item]),
        reader=reader,
        run_id="bundle-run",
    )

    assert report.validated_note_count == 1
    assert reader.calls == 1
    assert report.source_provider_call_count == 1
    assert report.literature_provider_call_count == 0
    bundle_sidecar = next(
        (tmp_path / "02_source_memory" / "bundles").glob("*.yml")
    )
    assert read_yaml(bundle_sidecar)["dependency_fingerprint"]
    profile = read_yaml(
        next((tmp_path / "02_source_memory" / "profiles").glob("*.yml"))
    )["profile"]
    assert profile["profile_schema_version"] == "1.3"
    assert profile["findings"] == []
    assert profile["context"]["profile_generation_route"] == "source_analysis_bundle"
    assert profile["context"]["source_analysis_bundle_dependency_fingerprint"]
    assert profile["context"]["thesis"] == "Grounded thesis; see page 1."
    assert profile["methods"][0] == "Comparative qualitative analysis."
    assert profile["concepts"] == ["credible commitment"]
    assert profile["theories"] == ["commitment problem"]
    assert profile["mechanisms"] == ["monitoring"]
    assert profile["outcomes"] == ["implementation"]
    assert profile["source_role"] == "journal article"
    assert profile["features"]["source_role"] == ["journal article"]
    assert profile["limitations"] == ["Grounded limitations; see page 1"]
    assert profile["boundaries"] == [
        "Can support: Grounded what_this_source_can_support; see page 1.",
        "Cannot support: Grounded what_this_source_cannot_support; see page 1.",
    ]
    assert profile["coverage"]["status"] == "partial"
    note = read_note(tmp_path / report.items[0]["note_path"])
    assert note["frontmatter"]["source_bundle_prompt_version"] == "21"


def test_atomic_note_projects_only_accepted_high_salience_quantitative_evidence(
    tmp_path,
) -> None:
    class QuantitativeZotero(FakeZotero):
        def fulltext(self, item_key):
            result = super().fulltext(item_key)
            assert result is not None
            result["content"] += (
                " In 2024, the reported outcome fell by 4 percentage points."
                " The source reports 42 treated cases and compares 17 controls."
                " The district's rank declined by 3 places to 21st."
            )
            return result

    class QuantitativeBundleReader(BundleReader):
        def read_source_bundle(self, text, metadata, question=None):
            payload = super().read_source_bundle(text, metadata, question)
            valid = deepcopy(payload["evidence_anchors"][0])
            valid.update(
                evidence_anchor_id="",
                claim="In 2024, the reported outcome fell.",
                salience_priority=10,
                quantitative_result={
                    "statistic": "case count",
                    "estimate": "4",
                    "unit": "percentage points",
                    "provenance": "source_reported",
                },
            )
            valid.pop("revision_hash", None)
            tied_duplicate = deepcopy(valid)
            tied_duplicate.update(
                evidence_anchor_id="",
                locator="p. 13",
                locators=["p. 13"],
            )
            lower_salience = deepcopy(valid)
            lower_salience.update(
                evidence_anchor_id="",
                claim="The source reports 17 controls.",
                salience_priority=9,
                quantitative_result={
                    "statistic": "control count",
                    "estimate": "17",
                    "unit": "controls",
                    "provenance": "source_reported",
                },
            )
            invalid = deepcopy(valid)
            invalid.update(
                evidence_anchor_id="",
                claim="The source reports 42 cases among 17 controls.",
                salience_priority=11,
            )
            payload["evidence_anchors"].extend(
                [valid, tied_duplicate, lower_salience, invalid]
            )
            for claim, estimate in (
                ("The district's rank declined by 3 places.", "3 places"),
                ("The district's resulting rank is 21st.", "21st"),
                ("The district's rank declined by 3 places to 21st.", "3 places"),
            ):
                split_rank = deepcopy(valid)
                split_rank.update(
                    claim=claim,
                    quantitative_result={
                        "estimate": estimate,
                        "provenance": "source_reported",
                    },
                )
                payload["evidence_anchors"].append(split_rank)
            return payload

    item = {
        "key": "ITEMA",
        "data": {
            "key": "ITEMA",
            "itemType": "journalArticle",
            "title": "Institutions and Reform",
            "date": "2024",
            "creators": [{"creatorType": "author", "lastName": "One"}],
        },
    }

    reader = QuantitativeBundleReader()
    client = QuantitativeZotero([item])
    report = run_map(
        MapRequest(tmp_path, provider="ollama", model="bundle-v1", parallel=1),
        client=client,
        reader=reader,
        run_id="quantitative-note-projection",
    )

    note_path = tmp_path / report.items[0]["note_path"]
    note = read_note(note_path)
    projection = (
        "In 2024, the reported outcome fell. Estimate: 4; Unit: percentage points."
    )
    assert note["body"].count(projection) == 1
    assert "The source reports 17 controls." not in note["body"]
    assert "42 cases among 17 controls" not in note["body"]
    assert "The district's rank declined by 3 places." in note["body"]
    assert "The district's resulting rank is 21st." in note["body"]
    assert "rank declined by 3 places to 21st" not in note["body"]
    profile = read_yaml(
        next((tmp_path / "02_source_memory" / "profiles").glob("*.yml"))
    )["profile"]
    rank_claims = {
        anchor["claim"] for anchor in profile["evidence_anchors"]
        if "district" in anchor["claim"]
    }
    assert rank_claims == {
        "The district's rank declined by 3 places.",
        "The district's resulting rank is 21st.",
    }
    bundle = read_yaml(
        next((tmp_path / "02_source_memory" / "bundles").glob("*.yml"))
    )["bundle"]
    assert sum(
        row["claim"] == "In 2024, the reported outcome fell."
        for row in bundle["evidence_anchors"]
    ) == 2
    assert bundle["analysis_sections"]["evidence_and_data"].count(projection) == 1
    before = (note["sha256"], note_path.stat().st_mtime_ns)

    replay = resume_map(
        tmp_path,
        "quantitative-note-projection",
        client=client,
        reader=reader,
    )

    assert replay.source_provider_call_count == report.source_provider_call_count == 1
    assert reader.calls == 1
    assert (read_note(note_path)["sha256"], note_path.stat().st_mtime_ns) == before


def test_source_calls_share_the_cumulative_profile_budget_and_replay_is_free(
    tmp_path,
) -> None:
    reader = BundleReader()
    request = MapRequest(tmp_path, provider="ollama", model="bundle-v1")
    budget_path = tmp_path / "provider_usage.yml"
    budget = _ProfileProviderBudget(budget_path, 1)
    metadata = {
        "_source_context": {
            "source_id": "source-zotero-A1",
            "zotero_key": "A1",
        }
    }
    checkpoint = tmp_path / "checkpoint"

    _read_document(
        reader,
        "First source.",
        metadata,
        None,
        request=request,
        checkpoint_root=checkpoint,
        provider_budget=budget,
    )
    _read_document(
        reader,
        "First source.",
        metadata,
        None,
        request=request,
        checkpoint_root=checkpoint,
        provider_budget=budget,
    )

    resumed = _ProfileProviderBudget(budget_path, 10)
    _read_document(
        reader,
        "Second source.",
        {
            "_source_context": {
                "source_id": "source-zotero-B2",
                "zotero_key": "B2",
            }
        },
        None,
        request=request,
        checkpoint_root=tmp_path / "second-checkpoint",
        provider_budget=resumed,
    )

    assert reader.calls == 2
    assert resumed.max_calls == 10
    assert resumed.cumulative_calls == 2
    resumed.flush()
    assert read_yaml(budget_path)["attempts"][0]["status"] == "completed"


def test_source_bundle_prompt_change_invalidates_committed_note_reuse(
    tmp_path, monkeypatch
) -> None:
    item = {
        "key": "ITEMA",
        "data": {
            "key": "ITEMA",
            "itemType": "journalArticle",
            "title": "Institutions and Reform",
            "date": "2024",
            "creators": [{"creatorType": "author", "lastName": "One"}],
        },
    }
    reader = BundleReader()
    request = MapRequest(
        tmp_path, provider="ollama", model="bundle-v1", parallel=1
    )
    run_map(
        request,
        client=FakeZotero([item]),
        reader=reader,
        run_id="bundle-prompt-one",
    )
    monkeypatch.setattr(
        "auto_zettelkasten.pipeline.SOURCE_BUNDLE_PROMPT_VERSION", "changed"
    )

    run_map(
        request,
        client=FakeZotero([item]),
        reader=reader,
        run_id="bundle-prompt-two",
    )

    assert reader.calls == 2


def test_unchanged_source_bundle_reuses_committed_note(tmp_path) -> None:
    item = {
        "key": "ITEMA",
        "data": {
            "key": "ITEMA",
            "itemType": "journalArticle",
            "title": "Institutions and Reform",
            "date": "2024",
            "creators": [{"creatorType": "author", "lastName": "One"}],
        },
    }
    reader = BundleReader()
    request = MapRequest(
        tmp_path, provider="ollama", model="bundle-v1", parallel=1
    )

    run_map(
        request,
        client=FakeZotero([item]),
        reader=reader,
        run_id="bundle-reuse-one",
    )
    replay = run_map(
        request,
        client=FakeZotero([item]),
        reader=reader,
        run_id="bundle-reuse-two",
    )

    assert reader.calls == 1
    assert replay.reused_count == 1


def test_stale_bundle_profile_is_refreshed_under_the_current_fingerprint(tmp_path) -> None:
    item = {
        "key": "ITEMA",
        "data": {
            "key": "ITEMA",
            "itemType": "journalArticle",
            "title": "Institutions and Reform",
            "date": "2024",
            "creators": [{"creatorType": "author", "lastName": "One"}],
        },
    }
    class EvidenceSurveyBundleReader(BundleReader):
        def read_source_bundle(self, text, metadata, question=None):
            payload = super().read_source_bundle(text, metadata, question)
            payload["analysis_sections"]["method_and_research_design"] = (
                "The article reports repeated polling."
            )
            payload["analysis_sections"]["evidence_and_data"] = (
                "The evidence comes from daily opt-in online surveys."
            )
            return payload

    reader = EvidenceSurveyBundleReader()
    request = MapRequest(tmp_path, provider="ollama", model="bundle-v1", parallel=1)
    run_map(
        request,
        client=FakeZotero([item]),
        reader=reader,
        run_id="stale-bundle-profile-first",
    )
    profile_path = next((tmp_path / "02_source_memory" / "profiles").glob("*.yml"))
    record = read_yaml(profile_path)
    record["profile"]["methods"] = ["Comparative qualitative analysis."]
    record["profile"]["validity"]["algorithm_version"] = "8"
    record["profile"]["dependency_hash"] = "stale-profile-dependency"
    write_yaml(profile_path, record)

    replay = run_map(
        request,
        client=FakeZotero([item]),
        reader=reader,
        run_id="stale-bundle-profile-replay",
    )

    refreshed = read_yaml(profile_path)["profile"]
    checkpoint = read_yaml(
        next(
            (
                tmp_path
                / "11_state"
                / "runs"
                / "stale-bundle-profile-replay"
                / "literature"
                / "profile_calls"
            ).glob("*.yml")
        )
    )
    assert reader.calls == 1
    assert replay.source_provider_call_count == 0
    assert refreshed["methods"] == [
        "Comparative qualitative analysis",
        "survey",
    ]
    assert refreshed["validity"]["algorithm_version"] == "9"
    assert refreshed["dependency_hash"] == checkpoint["fingerprint"]


def test_image_route_does_not_relabel_a_prior_bundle_as_reused(tmp_path) -> None:
    item = {
        "key": "ITEMA",
        "data": {
            "key": "ITEMA",
            "itemType": "journalArticle",
            "title": "Institutions and Reform",
            "date": "2024",
            "creators": [{"creatorType": "author", "lastName": "One"}],
        },
    }
    request = MapRequest(tmp_path, provider="ollama", model="bundle-v1", parallel=1)
    report = run_map(
        request,
        client=FakeZotero([item]),
        reader=BundleReader(),
        run_id="bundle-before-image-route",
    )
    note_path = tmp_path / report.items[0]["note_path"]
    frontmatter = read_note(note_path)["frontmatter"]
    row = {
        "item": item,
        "source_id": frontmatter["source_id"],
        "zotero_item_key": "ITEMA",
        "content_hash": frontmatter["inspected_content_hash"],
        "content_route": frontmatter["content_route"],
        "source_scope": frontmatter["source_scope"],
        "reader_provider": frontmatter["reader_provider"],
        "reader_model": frontmatter["reader_model"],
        "fingerprint": "new-image-route-fingerprint",
        "document_route": {"identity": "new-image-route"},
    }

    assert not _reusable_note(note_path, row, request)
    assert not _reusable_note(
        note_path,
        {**row, "content_route": "codex_pdf_page_images"},
        request,
    )


@pytest.mark.parametrize(
    ("prompt_version", "bundle_prompt_version", "should_reuse"),
    [("12", "7", True), ("13", "8", True), ("12", "8", False), ("13", "7", False)],
)
def test_legacy_source_contract_pairs_are_reused_fail_closed(
    tmp_path,
    prompt_version: str,
    bundle_prompt_version: str,
    should_reuse: bool,
) -> None:
    item = {
        "key": "ITEMA",
        "data": {
            "key": "ITEMA",
            "itemType": "journalArticle",
            "title": "Institutions and Reform",
            "date": "2024",
            "creators": [{"creatorType": "author", "lastName": "One"}],
        },
    }
    request = MapRequest(
        tmp_path, provider="ollama", model="bundle-v1", parallel=1
    )
    first_reader = BundleReader()
    first = run_map(
        request,
        client=FakeZotero([item]),
        reader=first_reader,
        run_id=f"legacy-{prompt_version}-{bundle_prompt_version}-first",
    )
    assert first_reader.calls == 1
    note_path = tmp_path / first.items[0]["note_path"]
    note_id = str(read_note(note_path)["frontmatter"]["note_id"])
    metadata_path = tmp_path / "11_state" / "note_metadata" / f"{note_id}.yml"
    metadata = read_yaml(metadata_path)
    legacy_dependency = f"legacy-{prompt_version}-{bundle_prompt_version}"
    metadata["frontmatter"].update(
        prompt_version=prompt_version,
        source_bundle_prompt_version=bundle_prompt_version,
        source_bundle_dependency_fingerprint=legacy_dependency,
    )
    write_yaml(metadata_path, metadata)
    bundle_path = next((tmp_path / "02_source_memory" / "bundles").glob("*.yml"))
    bundle = read_yaml(bundle_path)
    bundle["bundle"]["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "43,824",
        "period": "October 29, 2024",
        "provenance": "source_reported",
    }
    normalized_bundle = SourceAnalysisBundle.from_dict(bundle["bundle"])
    bundle["bundle"] = normalized_bundle.to_dict()
    bundle["semantic_fingerprint"] = stable_hash(
        normalized_bundle.semantic_dict()
    )
    bundle["dependency_fingerprint"] = legacy_dependency
    write_yaml(bundle_path, bundle)
    note_before = note_path.read_bytes()
    bundle_payload_before = deepcopy(bundle["bundle"])
    semantic_fingerprint_before = bundle["semantic_fingerprint"]
    profile_path = next((tmp_path / "02_source_memory" / "profiles").glob("*.yml"))
    profile_record = read_yaml(profile_path)
    expected_profile = profile_record["profile"]
    stale_profile = deepcopy(expected_profile)
    stale_profile["features"]["source_role"] = ["stale-role"]
    stale_profile["limitations"] = []
    write_yaml(profile_path, {**profile_record, "profile": stale_profile})

    replay_reader = BundleReader()
    replay = run_map(
        request,
        client=FakeZotero([item]),
        reader=replay_reader,
        run_id=f"legacy-{prompt_version}-{bundle_prompt_version}-replay",
    )

    assert replay.reused_count == int(should_reuse)
    assert replay_reader.calls == int(not should_reuse)
    if not should_reuse:
        return
    assert replay.source_provider_call_count == 0
    assert replay.provider_call_count == 0
    assert replay.items[0]["reason"] == "fingerprint_match"
    assert note_path.read_bytes() == note_before
    replayed_metadata = read_yaml(metadata_path)["frontmatter"]
    assert replayed_metadata["prompt_version"] == prompt_version
    assert (
        replayed_metadata["source_bundle_prompt_version"]
        == bundle_prompt_version
    )
    migrated_bundle = read_yaml(bundle_path)
    assert migrated_bundle["dependency_fingerprint"] != legacy_dependency
    assert migrated_bundle["bundle"] == bundle_payload_before
    assert migrated_bundle["semantic_fingerprint"] == semantic_fingerprint_before
    repaired_profile = read_yaml(profile_path)["profile"]
    assert repaired_profile["features"]["source_role"] == expected_profile["features"][
        "source_role"
    ]
    assert repaired_profile["limitations"] == expected_profile["limitations"]


def test_old_bundle_diagnostics_migrate_locally_without_rewriting_note(
    tmp_path,
) -> None:
    class QuantitativeZotero(FakeZotero):
        def fulltext(self, item_key):
            result = dict(super().fulltext(item_key))
            result["content"] += " Reported estimate: 42."
            return result

    item = {
        "key": "ITEMA",
        "data": {
            "key": "ITEMA",
            "itemType": "journalArticle",
            "title": "Institutions and Reform",
            "date": "2024",
            "creators": [{"creatorType": "author", "lastName": "One"}],
        },
    }
    reader = BundleReader()
    request = MapRequest(
        tmp_path, provider="ollama", model="bundle-v1", parallel=1
    )
    first = run_map(
        request,
        client=QuantitativeZotero([item]),
        reader=reader,
        run_id="bundle-local-migration-one",
    )
    note_path = tmp_path / first.items[0]["note_path"]
    note_before = note_path.read_text(encoding="utf-8")
    bundle_path = next(
        (tmp_path / "02_source_memory" / "bundles").glob("*.yml")
    )
    sidecar = read_yaml(bundle_path)
    raw_anchor = dict(sidecar["bundle"]["evidence_anchors"][0])
    raw_anchor["salience_priority"] = "high"
    raw_anchor["quantitative_result"] = {
        "estimate": 42,
        "provenance": "Reported in the source.",
    }
    sidecar["bundle"]["evidence_anchors"] = []
    sidecar["bundle"]["component_diagnostics"] = [
        {
            "component": "evidence_anchors",
            "reason": "legacy optional-row parse failure",
            "raw": raw_anchor,
        }
    ]
    sidecar["dependency_fingerprint"] = "legacy-normalization-v8"
    write_yaml(bundle_path, sidecar)

    replay = run_map(
        request,
        client=QuantitativeZotero([item]),
        reader=reader,
        run_id="bundle-local-migration-two",
    )

    migrated = read_yaml(bundle_path)
    assert reader.calls == 1
    assert replay.reused_count == 1
    assert migrated["dependency_fingerprint"] != "legacy-normalization-v8"
    assert len(migrated["bundle"]["evidence_anchors"]) == 1
    assert (
        migrated["bundle"]["evidence_anchors"][0]["quantitative_result"]["estimate"]
        == "42"
    )
    assert note_path.read_text(encoding="utf-8") == note_before


def test_auto_provider_concurrency_runs_all_ready_source_calls(
    tmp_path,
) -> None:
    barrier = threading.Barrier(4)

    class ConcurrentBundleReader(BundleReader):
        def read_source_bundle(self, text, metadata, question=None):
            barrier.wait(timeout=3)
            return super().read_source_bundle(text, metadata, question)

    items = [
        {
            "key": f"ITEM{index}",
            "data": {
                "key": f"ITEM{index}",
                "itemType": "journalArticle",
                "title": f"Source {index}",
                "date": "2024",
                "creators": [
                    {"creatorType": "author", "lastName": f"Author{index}"}
                ],
            },
        }
        for index in range(4)
    ]
    reader = ConcurrentBundleReader()

    report = run_map(
        MapRequest(
            tmp_path,
            provider="ollama",
            model="bundle-v1",
            provider_concurrency="auto",
        ),
        client=FakeZotero(items),
        reader=reader,
        run_id="concurrent-source-bundles",
    )

    assert reader.calls == 4
    assert report.source_peak_concurrency == 4
    assert report.source_stage_wall_seconds > 0


def test_auto_source_concurrency_is_bounded_for_local_extraction_safety(
    tmp_path,
) -> None:
    barrier = threading.Barrier(32)

    class CloudBundleReader(BundleReader):
        is_cloud = True

        def read_source_bundle(self, text, metadata, question=None):
            if self.calls < 32:
                barrier.wait(timeout=5)
            return super().read_source_bundle(text, metadata, question)

    items = [
        {
            "key": f"ITEM{index}",
            "data": {
                "key": f"ITEM{index}",
                "itemType": "journalArticle",
                "title": f"Source {index}",
                "date": "2024",
                "creators": [
                    {"creatorType": "author", "lastName": f"Author{index}"}
                ],
            },
        }
        for index in range(33)
    ]

    report = run_map(
        MapRequest(
            tmp_path,
            provider="deepseek",
            model="bundle-v1",
            allow_cloud=True,
            provider_concurrency="auto",
        ),
        client=FakeZotero(items),
        reader=CloudBundleReader(),
        run_id="bounded-concurrent-source-bundles",
    )

    assert report.validated_note_count == 33
    assert report.source_peak_concurrency == 32


def test_truncated_direct_bundle_does_not_start_hierarchical_calls(tmp_path) -> None:
    class TruncatedReader:
        name = "truncated"
        model = "bundle-v1"
        is_cloud = False

        def read_source_bundle(self, text, metadata, question=None):
            del text, metadata, question
            raise RuntimeError("provider response ended with finish_reason=length")

        def summarize_chunk(self, *args, **kwargs):
            raise AssertionError("truncation must not start chunk calls")

    with pytest.raises(RuntimeError, match="finish_reason=length"):
        _read_document(
            TruncatedReader(),
            "An ordinary report that fits in the direct input budget.",
            {
                "_source_context": {
                    "source_id": "source-zotero-A1",
                    "zotero_key": "A1",
                }
            },
            None,
            request=MapRequest(tmp_path, provider="ollama", model="bundle-v1"),
            checkpoint_root=tmp_path / "checkpoint",
        )


def test_bundle_preflight_routes_directly_to_one_chunk_and_bundle_synthesis(
    tmp_path,
) -> None:
    class HierarchicalBundleReader(BundleReader):
        def __init__(self) -> None:
            super().__init__()
            self.chunk_calls = 0
            self.synthesis_tokens = 0

        def should_read_source_bundle_directly(self, *_args, **_kwargs):
            return False

        def read_source_bundle(self, *_args, **_kwargs):
            raise AssertionError("direct bundle call must be skipped")

        def summarize_chunk(self, *_args, **_kwargs):
            self.chunk_calls += 1
            return {"summary": "Grounded chunk evidence."}

        def synthesize_document_bundle(
            self, _chunks, metadata, _question=None, **kwargs
        ):
            self.synthesis_tokens = kwargs["max_output_tokens"]
            payload = _bundle_payload()
            payload["source_identity"] = {
                "source_id": metadata["_source_context"]["source_id"],
                "zotero_key": metadata["_source_context"]["zotero_key"],
            }
            return payload

    reader = HierarchicalBundleReader()
    result, route, _reason = _read_document(
        reader,
        "A report that the exact bundle prompt budget rejects.",
        {
            "_source_context": {
                "source_id": "source-zotero-A1",
                "zotero_key": "A1",
            }
        },
        None,
        request=MapRequest(tmp_path, provider="ollama", model="bundle-v1"),
        checkpoint_root=tmp_path / "checkpoint",
    )

    assert result["bundle_schema_version"] == "1"
    assert route == "bundle-reader_hierarchical_text"
    assert reader.chunk_calls == 1
    assert reader.synthesis_tokens == 64_000


def test_context_budget_admission_error_falls_back_to_hierarchical_reading(
    tmp_path,
) -> None:
    class AdmissionReader:
        name = "admission"
        model = "bundle-v1"
        is_cloud = False

        def read_source_bundle(self, *_args, **_kwargs):
            raise ProviderError("source analysis bundle exceeds context budget")

        def summarize_chunk(self, *_args, **_kwargs):
            return {"summary": "Grounded chunk evidence."}

        def synthesize_document_bundle(self, _chunks, _metadata, *_args, **_kwargs):
            return _bundle_payload()

    result, route, _reason = _read_document(
        AdmissionReader(),
        "A short source.",
        {
            "_source_context": {
                "source_id": "source-zotero-A1",
                "zotero_key": "A1",
            }
        },
        None,
        request=MapRequest(tmp_path, provider="ollama", model="bundle-v1"),
        checkpoint_root=tmp_path / "checkpoint",
    )

    assert result["bundle_schema_version"] == "1"
    assert route == "admission_hierarchical_text"


def test_hierarchical_bundle_honors_the_supported_32k_output(
    monkeypatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    captured: list[int] = []
    reader = DeepSeekReader(allow_cloud=True, max_output_tokens=6_000)

    def generate(_system, _user, output_tokens, _deadline):
        captured.append(output_tokens)
        return json.dumps(_bundle_payload())

    monkeypatch.setattr(reader, "_generate_text", generate)
    reader.synthesize_document_bundle(
        [{"finding": "Grounded memo."}],
        {
            "_source_context": {
                "source_id": "source-zotero-A1",
                "zotero_key": "A1",
            }
        },
        max_output_tokens=32_000,
    )

    assert captured == [32_000]


class WrongSourceBundleReader(BundleReader):
    def read_source_bundle(self, text, metadata, question=None):
        payload = super().read_source_bundle(text, metadata, question)
        payload["evidence_anchors"][0]["source_id"] = "source-zotero-wrong"
        return payload


def test_wrong_source_bundle_is_parked_without_publishing_a_note(tmp_path) -> None:
    item = {
        "key": "ITEMA",
        "data": {
            "key": "ITEMA",
            "itemType": "journalArticle",
            "title": "Institutions and Reform",
            "date": "2024",
            "creators": [{"creatorType": "author", "lastName": "One"}],
        },
    }
    report = run_map(
        MapRequest(tmp_path, provider="ollama", model="bundle-v1", parallel=1),
        client=FakeZotero([item]),
        reader=WrongSourceBundleReader(),
        run_id="wrong-source-bundle",
    )

    assert report.validated_note_count == 0
    assert report.parked_for_review_count == 1
    assert report.items[0]["reason"].startswith(
        "source_bundle_ownership_invalid:evidence_anchors.source_id"
    )
    assert list((tmp_path / "01_atomic_notes").glob("*.md")) == []
    failure = read_yaml(
        tmp_path
        / "11_state"
        / "runs"
        / "wrong-source-bundle"
        / "items"
        / "ITEMA"
        / "source_failure.yml"
    )
    assert failure["status"] == "parked_for_review"
    assert failure["raw_response"]["bundle_schema_version"] == "1"


@pytest.mark.parametrize(
    ("source_text", "quantitative_result", "reason"),
    [
        (
            """Every hour:

15 people are killed.

35 people are injured.

42 bombs are dropped*.

12 buildings are destroyed.

* Based on the first six days of the war.
""",
            {
                "estimate": "15 killed/hour; 35 injured/hour; 42 bombs/hour; 12 buildings/hour",
                "period": "First six days of the war",
                "provenance": "source_reported",
            },
            "footnote_scope_combines_marked_and_unmarked_quantities",
        ),
        (
            """Deaths reported: 43,824.
Published On 9 Oct 2023
Updated:
29 Oct 2024
Background.
Further context.
Separate table.
Here are figures as of October 29, 2024.
Killed: 43,061.
""",
            {
                "estimate": "43,824",
                "period": "October 29, 2024",
                "provenance": "source_reported",
            },
            "period_date_not_local_to_reported_estimate",
        ),
    ],
)
def test_quantitative_provenance_failure_parks_without_publishing(
    tmp_path, source_text, quantitative_result, reason
) -> None:
    class QuantitativeBundleReader(BundleReader):
        def read_source_bundle(self, text, metadata, question=None):
            payload = super().read_source_bundle(text, metadata, question)
            payload["evidence_anchors"][0]["quantitative_result"] = (
                quantitative_result
            )
            return payload

    class SourceZotero(FakeZotero):
        def fulltext(self, item_key):
            self.fulltext_calls += 1
            return {
                "content": source_text + (" Grounded source context." * 40),
                "contentType": "text/plain",
            }

    item = {
        "key": "ITEMA",
        "data": {
            "key": "ITEMA",
            "itemType": "report",
            "title": "Quantitative report",
            "date": "2024",
        },
    }
    reader = QuantitativeBundleReader()

    report = run_map(
        MapRequest(tmp_path, provider="ollama", model="bundle-v1", parallel=1),
        client=SourceZotero([item]),
        reader=reader,
        run_id=f"invalid-quantitative-{reason}",
    )

    assert reader.calls == 1
    assert report.validated_note_count == 0
    assert report.parked_for_review_count == 1
    assert report.items[0]["reason"] == (
        f"source_bundle_quantitative_provenance_invalid:{reason}"
    )
    assert list((tmp_path / "01_atomic_notes").glob("*.md")) == []
    assert list((tmp_path / "02_source_memory" / "bundles").glob("*.yml")) == []
    assert list((tmp_path / "02_source_memory" / "profiles").glob("*.yml")) == []


def test_quantitative_provenance_accepts_split_footnote_and_local_body_date() -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "42 bombs/hour",
        "period": "First six days of the war",
        "provenance": "source_reported",
    }
    row = {
        "source_id": "source-zotero-A1",
        "zotero_item_key": "A1",
        "text": "Every hour:\n15 killed.\n42 bombs dropped*.\n*Based on the first six days.\n",
    }

    assert _source_bundle_from_result(payload, row, "full_document") is not None

    row["text"] = (
        "Every hour:\n\n"
        "15 killed.\n\n"
        "42 bombs dropped*.\n\n"
        "12 buildings destroyed.\n\n"
        "* Based on the first six days.\n"
    )
    assert _source_bundle_from_result(payload, row, "full_document") is not None

    row["text"] = (
        "Every hour:\n15 killed; 42 bombs dropped*.\n"
        "*Based on the first six days of the war."
    )
    assert _source_bundle_from_result(payload, row, "full_document") is not None

    row["text"] = (
        "Every hour:\n15 killed; 42 bombs dropped*.\n"
        "*Based on the first six days of the war, according to the agency."
    )
    assert _source_bundle_from_result(payload, row, "full_document") is not None

    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "43,061 killed",
        "period": "October 29, 2024",
        "provenance": "source_reported",
    }
    row["text"] = "Figures as of October 29, 2024:\nKilled: 43,061."
    assert _source_bundle_from_result(payload, row, "full_document") is not None

    row["text"] = (
        "Table totals as of October 29, 2024:\n"
        "Category A\nCategory B\nCategory C\nCategory D\nCategory E\nCategory F\n"
        "Killed: 43,061."
    )
    assert _source_bundle_from_result(payload, row, "full_document") is not None

    row["text"] = "On 2024-10-29, the reported total was 43,061."
    assert _source_bundle_from_result(payload, row, "full_document") is not None

    payload["evidence_anchors"][0]["quantitative_result"]["period"] = (
        "October 29"
    )
    row["text"] = (
        "Updated:\nOctober 29, 2024\n\n"
        "Here are the figures as of October 29:\n\n"
        "Killed: 43,061."
    )
    assert _source_bundle_from_result(payload, row, "full_document") is not None

    payload["evidence_anchors"][0]["quantitative_result"]["period"] = (
        "October 29, 2024"
    )
    row["text"] = (
        "Table totals as of October 29, 2024:\n"
        + "".join(f"Category {index}\n" for index in range(20))
        + "Killed: 43,061."
    )
    assert _source_bundle_from_result(payload, row, "full_document") is not None


@pytest.mark.parametrize(
    ("source_text", "claim", "estimate"),
    [
        (
            "Th e p ro gram allocated $ 48 7 m illio n fo r lo cal grants.",
            "The program allocated $487 million for local grants.",
            "$487 million",
        ),
        (
            "Th e p ro gram supported 57 0,000 h ouseholds ac ross regions.",
            "The program supported 570,000 households across regions.",
            "570,000",
        ),
    ],
)
def test_quantitative_provenance_accepts_split_pdf_digit_glyphs(
    source_text: str, claim: str, estimate: str
) -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["claim"] = claim
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": estimate,
        "provenance": "source_reported",
    }

    assert (
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": source_text,
            },
            "full_document",
        )
        is not None
    )


@pytest.mark.parametrize(
    "source_text",
    [
        "Table row 26 0",
        "Model A B C D outcome estimate dollars 26 0",
        "Model A B C D outcome estimate dollars $26 0",
        "Table: 26  0",
        "The values were 26 0 in adjacent columns.",
    ],
)
def test_quantitative_provenance_does_not_join_separate_plain_numbers(
    source_text: str,
) -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["claim"] = "The total was 260 cases."
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "260",
        "provenance": "source_reported",
    }

    with pytest.raises(
        SourceBundleQuantitativeProvenanceError,
        match="reported_estimate_not_found_in_source",
    ):
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": source_text,
            },
            "full_document",
        )


@pytest.mark.parametrize(
    "period", ["", "Entire war", "First 60 days", "First six weeks"]
)
def test_quantitative_provenance_requires_marked_footnote_scope(period) -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "42 bombs/hour",
        "period": period,
        "provenance": "source_reported",
    }
    row = {
        "source_id": "source-zotero-A1",
        "zotero_item_key": "A1",
        "text": (
            "Every hour:\n15 killed; 42 bombs dropped*.\n"
            "*Based on the first six days of the war."
        ),
    }

    with pytest.raises(
        SourceBundleQuantitativeProvenanceError,
        match="footnote_scope_combines_marked_and_unmarked_quantities",
    ):
        _source_bundle_from_result(payload, row, "full_document")


@pytest.mark.parametrize(
    ("text", "estimate", "period"),
    [
        (
            "15 killed, 42 bombs*.\n*Based on the first six days.",
            "15 killed/hour",
            "First six days",
        ),
        (
            "42 bombs*.\n*Based on the opening phase.",
            "42 bombs/hour",
            "",
        ),
        (
            "42 bombs*.\n*As of October 29, 2024.",
            "42 bombs/hour",
            "",
        ),
        (
            "42 bombs[1].\n[1] Based on the opening phase.",
            "42 bombs/hour",
            "",
        ),
        (
            "42 bombs¹.\n¹ Based on the opening phase.",
            "42 bombs/hour",
            "",
        ),
        (
            "42 bombs^1.\n^1 Based on the opening phase.",
            "42 bombs/hour",
            "",
        ),
        (
            "15 killed and 42 bombs*.\n*Based on the first six days.",
            "15 killed/hour",
            "First six days",
        ),
        (
            "15 killed / 42 bombs*.\n*Based on the first six days.",
            "15 killed/hour",
            "First six days",
        ),
        (
            "15 killed/42 bombs*.\n*Based on the first six days.",
            "15 killed/hour",
            "First six days",
        ),
        (
            "15 killed/ 42 bombs*.\n*Based on the first six days.",
            "15 killed/hour",
            "First six days",
        ),
        (
            "15 killed /42 bombs*.\n*Based on the first six days.",
            "15 killed/hour",
            "First six days",
        ),
        (
            "15 killed & 42 bombs*.\n*Based on the first six days.",
            "15 killed/hour",
            "First six days",
        ),
        (
            "42 bombs/hour*.\n*Based on the first six days.",
            "42 bombs/hour",
            "",
        ),
        (
            "42/100 participants*.\n*Based on the first six days.",
            "42/100 participants",
            "",
        ),
        (
            "15 killed/hour and 42 bombs/hour*.\n*Based on the first six days.",
            "15 killed/hour",
            "First six days",
        ),
        (
            "15 killed,42 bombs*.\n*Based on the first six days.",
            "15 killed/hour",
            "First six days",
        ),
        (
            "15 killed or 42 bombs*.\n*Based on the first six days.",
            "15 killed/hour",
            "First six days",
        ),
        (
            "15 killed plus 42 bombs*.\n*Based on the first six days.",
            "15 killed/hour",
            "First six days",
        ),
        (
            "42 bombs(1).\n(1) Based on the first six days.",
            "42 bombs/hour",
            "",
        ),
        (
            "15 killed* and 42 bombs*.\n*Based on the first six days.",
            "15 killed/hour",
            "",
        ),
        (
            "42 bombs* were dropped.\n*Based on the first six days.",
            "42 bombs/hour",
            "",
        ),
    ],
)
def test_quantitative_provenance_preserves_exact_footnote_scope(
    text, estimate, period
) -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": estimate,
        "period": period,
        "provenance": "source_reported",
    }

    with pytest.raises(
        SourceBundleQuantitativeProvenanceError,
        match="footnote_scope_combines_marked_and_unmarked_quantities",
    ):
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": text,
            },
            "full_document",
        )


def test_quantitative_provenance_accepts_equivalent_numbered_day_scope() -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "42 bombs/hour",
        "period": "Days 1–6",
        "provenance": "source_reported",
    }
    assert (
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": "42 bombs*.\n*Based on the first six days.",
            },
            "full_document",
        )
        is not None
    )


@pytest.mark.parametrize(
    "estimate", ["42 bombs/hour", "42/100 participants"]
)
def test_quantitative_provenance_keeps_rate_and_ratio_footnotes_whole(
    estimate,
) -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": estimate,
        "period": "First six days",
        "provenance": "source_reported",
    }

    assert (
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": f"{estimate}*.\n* Based on the first six days.",
            },
            "full_document",
        )
        is not None
    )


def test_quantitative_provenance_does_not_borrow_year_from_metadata() -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "43,061",
        "period": "October 29, 2024",
        "provenance": "source_reported",
    }

    with pytest.raises(
        SourceBundleQuantitativeProvenanceError,
        match="period_date_not_found_in_source",
    ):
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": (
                    "Updated: October 29, 2024\n\n"
                    "Here are the figures as of October 29:\n43,061 killed."
                ),
            },
            "full_document",
        )

    for estimate, text in (
        (
            "1 percentage point change",
            "Results changed from 2023 to 2024.",
        ),
        (
            "9 years change",
            "The case count dropped from 40 to 31.",
        ),
        (
            "50 years",
            "The study had 10 groups with 5 cases each.",
        ),
    ):
        payload["evidence_anchors"][0]["quantitative_result"] = {
            "estimate": estimate,
            "period": "",
            "provenance": "system_derived",
        }
        with pytest.raises(
            SourceBundleQuantitativeProvenanceError,
            match="derived_estimate_not_supported_by_source_inputs",
        ):
            _source_bundle_from_result(
                payload,
                {
                    "source_id": "source-zotero-A1",
                    "zotero_item_key": "A1",
                    "text": text,
                },
                "full_document",
            )

    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "5 total cases",
        "period": "October 29, 2024",
        "provenance": "system_derived",
    }
    with pytest.raises(
        SourceBundleQuantitativeProvenanceError,
        match="derived_estimate_not_supported_by_source_inputs",
    ):
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": "On October 29, 2024, the study covered 2 countries over 3 years.",
            },
            "full_document",
        )


@pytest.mark.parametrize(
    ("estimate", "text"),
    [
        ("29 cases", "On October 29, 2024, the source reports 29 cases."),
        (
            "2024 respondents",
            "On October 29, 2024, the source reports 2024 respondents.",
        ),
        ("0.42", "On October 29, 2024, the estimate was .42."),
        ("-0.42", "On October 29, 2024, the estimate was -.42."),
        ("42%", "On October 29, 2024, the estimate was 42 percent."),
        ("43,824", "On October 29, 2024, the total was 43\u202f824."),
        ("-5%", "On October 29, 2024, the estimate was −5%."),
        ("15%", "On October 29, 2024, the estimate was 15 %."),
        ("1 staff; 2 students", "On October 29, 2024, one staff member barred two students."),
        ("-0.07", "On October 29, 2024, the scores were +0.90-0.07."),
        ("1%-3%", "On October 29, 2024, the interval was 1–3 percent."),
        (
            "1,000–1,500 cases",
            "On October 29, 2024, the range was 1,000-1,500 cases.",
        ),
        ("600–800 cases", "On October 29, 2024, the range was 600-800 cases."),
        ("215–184 cases", "On October 29, 2024, the range was 215-184 cases."),
    ],
)
def test_quantitative_provenance_normalizes_estimate_tokens(
    estimate, text
) -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": estimate,
        "period": "October 29, 2024",
        "provenance": "source_reported",
    }

    assert (
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": text,
            },
            "full_document",
        )
        is not None
    )


def test_quantitative_provenance_rejects_reversed_derived_direction() -> None:
    payload = _bundle_payload()

    for estimate, text in (
        (
            "9 percentage points increase",
            "On October 29, 2024, the rate fell from 40% to 31%.",
        ),
        (
            "22.5% relative increase",
            "On October 29, 2024, the rate fell from 40% to 31%.",
        ),
        (
            "4.5 score points decrease",
            "Scores were 71.8 in 2024, improving on 67.3 in 2023.",
        ),
        (
            "9 percentage points reduction",
            "On October 29, 2024, the rate rose from 31% to 40%.",
        ),
        (
            "40% of countries had cases",
            "On October 29, 2024, 2 cases of 5 countries were examined.",
        ),
        (
            "40% of countries had cases",
            "On October 29, 2024, 2 cases among 5 countries were examined.",
        ),
        (
            "5 cases increase; 5 cases decrease",
            "On October 29, 2024, group A increased from 10 to 15 cases; "
            "group B increased from 8 to 13 cases.",
        ),
        (
            "5 cases increase and 5 cases decrease",
            "On October 29, 2024, group A increased from 10 to 15 cases; "
            "group B increased from 8 to 13 cases.",
        ),
        (
            "5 cases increase and 5 countries total",
            "On October 29, 2024, group A increased from 10 to 15 cases.",
        ),
        (
            "5 cases increase, 5 cases decrease",
            "On October 29, 2024, group A increased from 10 to 15 cases; "
            "group B increased from 8 to 13 cases.",
        ),
        (
            "5 cases increase / 5 cases decrease",
            "On October 29, 2024, group A increased from 10 to 15 cases; "
            "group B increased from 8 to 13 cases.",
        ),
        (
            "5 percentage points decrease",
            "On October 29, 2024, five countries were sampled and the rate "
            "rose from 10% to 15%.",
        ),
        (
            "5 cases increase/decrease",
            "Cases rose from 10 to 15.",
        ),
        (
            "5 cases increase or decrease",
            "Cases rose from 10 to 15.",
        ),
        (
            "5 cases higher/lower",
            "Cases rose from 10 to 15.",
        ),
    ):
        payload["evidence_anchors"][0]["quantitative_result"] = {
            "estimate": estimate,
            "period": "",
            "provenance": "system_derived",
        }
        with pytest.raises(
            SourceBundleQuantitativeProvenanceError,
            match="derived_estimate_not_supported_by_source_inputs",
        ):
            _source_bundle_from_result(
                payload,
                {
                    "source_id": "source-zotero-A1",
                    "zotero_item_key": "A1",
                    "text": text,
                },
                "full_document",
            )


@pytest.mark.parametrize(
    "text",
    [
        "Updated estimates on October 29, 2024 were 43,061.",
        "Updated October 29, 2024 total: 43,061 deaths as of that date.",
        "Published results on October 29, 2024 reported 43,061 cases.",
        "Retrieved records on October 29, 2024 contained 43,061 cases.",
        (
            "Updated: October 29, 2024\n"
            "On October 29, 2024, the source reported 43,061 deaths."
        ),
        "Updated:\nOn October 29, 2024, the source reported 43,061 deaths.",
    ],
)
def test_quantitative_provenance_keeps_body_date_language(text) -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "43,061",
        "period": "October 29, 2024",
        "provenance": "source_reported",
    }

    assert (
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": text,
            },
            "full_document",
        )
        is not None
    )


def test_quantitative_provenance_preserves_unicode_minus_direction() -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "+5%",
        "period": "October 29, 2024",
        "provenance": "source_reported",
    }

    with pytest.raises(
        SourceBundleQuantitativeProvenanceError,
        match="reported_estimate_not_found_in_source",
    ):
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": "On October 29, 2024, the estimate was −5%.",
            },
            "full_document",
        )


def test_quantitative_provenance_requires_every_period_endpoint() -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "43,061 killed",
        "period": "October 1, 2024 through October 29, 2024",
        "provenance": "source_reported",
    }
    with pytest.raises(
        SourceBundleQuantitativeProvenanceError,
        match="period_date_not_found_in_source",
    ):
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": "Figures as of October 29, 2024:\nKilled: 43,061.",
            },
            "full_document",
        )


@pytest.mark.parametrize(
    ("period", "source_text"),
    [
        ("2023–2024", "Scores for 2023–2024: 5 cases."),
        ("2023 to 2024", "Scores from 2023 to 2024: 5 cases."),
        ("2023 versus 2024", "Scores for 2023 versus 2024: 5 cases."),
    ],
)
def test_quantitative_provenance_accepts_local_year_ranges(
    period, source_text
) -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "5 cases",
        "period": period,
        "provenance": "source_reported",
    }

    assert (
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": source_text,
            },
            "full_document",
        )
        is not None
    )


@pytest.mark.parametrize("provenance", ["unknown", "system_derived"])
def test_quantitative_provenance_label_cannot_bypass_validation(provenance) -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "43,824",
        "period": "October 29, 2024",
        "provenance": provenance,
    }
    with pytest.raises(
        SourceBundleQuantitativeProvenanceError,
        match="period_date_not_found_in_source",
    ):
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": "Deaths: 43,824.\nUpdated:\n29 Oct 2024.",
            },
            "full_document",
        )


@pytest.mark.parametrize(
    "metadata",
    [
        "Updated: October 29, 2024 at 10:00 AM EDT",
        "Published On: Tuesday, October 29, 2024 at 10:00 a.m. ET",
        "Last Updated October 29, 2024, 10 AM Eastern Time",
        "Updated: October 29, 2024 at 22:30 GMT",
        "Updated: 2024-10-29T22:30:00Z",
        "Published On October 29, 2024 at 10:00 UTC+02:00",
        "Updated by Alice Smith on October 29, 2024",
        "Updated:\nBy Alice Smith\nOctober 29, 2024",
        "Updated by Staff:\nOctober 29, 2024",
        "Last updated by Staff at 10:00 AM:\nOctober 29, 2024",
        "Updated:\nBy Staff\n10:00 AM\nUTC\nOctober 29, 2024",
        "Updated\nBy\nStaff\nat\n10 AM\nOctober 29, 2024",
        "Updated 2 hours ago: October 29, 2024",
        "Date published: October 29, 2024",
        "Publication date: October 29, 2024",
        "Last modified: October 29, 2024",
    ],
)
def test_quantitative_provenance_rejects_metadata_boilerplate_dates(
    metadata,
) -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "43,824",
        "period": "October 29, 2024",
        "provenance": "source_reported",
    }

    with pytest.raises(
        SourceBundleQuantitativeProvenanceError,
        match="period_date_not_found_in_source",
    ):
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": f"Deaths: 43,824.\n{metadata}",
            },
            "full_document",
        )


@pytest.mark.parametrize(
    ("field", "value", "text"),
    [
        ("estimate", "2024 respondents", "Published: 2024"),
        ("sample", "2024 respondents", "Published: 2024"),
        (
            "estimate",
            "10 participants",
            "Updated: October 29, 2024 at 10:00 AM EDT",
        ),
        (
            "estimate",
            "10 participants",
            "Updated:\n29 Oct 2024\n10:00 AM (GMT)",
        ),
        (
            "estimate",
            "2 participants",
            "Updated:\n29 Oct 2024\n02:02 (GMT)",
        ),
    ],
)
def test_quantitative_provenance_rejects_metadata_only_quantity(
    field, value, text
) -> None:
    payload = _bundle_payload()
    result = {
        "estimate": "",
        "provenance": "source_reported",
        field: value,
    }
    payload["evidence_anchors"][0]["quantitative_result"] = result

    with pytest.raises(
        SourceBundleQuantitativeProvenanceError,
        match="reported_estimate_not_found_in_source",
    ):
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": f"{text}\nNo respondents were reported.",
            },
            "full_document",
        )


def test_quantitative_provenance_accepts_simple_system_derivation() -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "9 percentage points decrease; 22.5% relative decrease",
        "period": "October 29, 2024",
        "provenance": "system_derived",
    }

    assert (
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": (
                    "On October 29, 2024, the reported rate fell from 40% to 31%."
                ),
            },
            "full_document",
        )
        is not None
    )

    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "10 total cases",
        "period": "October 29, 2024",
        "provenance": "system_derived",
    }
    row = {
        "source_id": "source-zotero-A1",
        "zotero_item_key": "A1",
        "text": (
            "On October 29, 2024, group A had 5 cases and group B had 5 cases."
        ),
    }
    assert _source_bundle_from_result(payload, row, "full_document") is not None

    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "25%",
        "period": "October 29, 2024",
        "provenance": "system_derived",
    }
    row["text"] = "On October 29, 2024, one group contained 1 of 4 cases."
    assert _source_bundle_from_result(payload, row, "full_document") is not None

    row["text"] = "On October 29, 2024, the subset contained 1 case out of 4 cases."
    assert _source_bundle_from_result(payload, row, "full_document") is not None

    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "40%",
        "period": "October 29, 2024",
        "provenance": "system_derived",
    }
    row["text"] = "On October 29, 2024, the subset contained 2 of 5 countries."
    assert _source_bundle_from_result(payload, row, "full_document") is not None

    row["text"] = "On October 29, 2024, the subset contained 2 of the 5 countries."
    assert _source_bundle_from_result(payload, row, "full_document") is not None

    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "3 total cases",
        "period": "2024",
        "provenance": "system_derived",
    }
    row["text"] = "In 2024, group A had one case and group B had two cases."
    assert _source_bundle_from_result(payload, row, "full_document") is not None

    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "22.5% relative decrease",
        "period": "October 29, 2024",
        "provenance": "system_derived",
    }
    row["text"] = "On October 29, 2024, cases fell from 40 to 31."
    assert _source_bundle_from_result(payload, row, "full_document") is not None

    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "9 cases decrease",
        "period": "October 29, 2024",
        "provenance": "system_derived",
    }
    row["text"] = (
        "On October 29, 2024, cases decreased from 40 total cases "
        "to 31 total cases."
    )
    assert _source_bundle_from_result(payload, row, "full_document") is not None

    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "2 cases per group",
        "period": "October 29, 2024",
        "provenance": "system_derived",
    }
    row["text"] = "On October 29, 2024, there were 10 cases among 5 groups."
    assert _source_bundle_from_result(payload, row, "full_document") is not None

    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "12 total cases",
        "period": "October 29, 2024",
        "provenance": "system_derived",
    }
    row["text"] = "On October 29, 2024, 3 groups with 4 cases each."
    assert _source_bundle_from_result(payload, row, "full_document") is not None

    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "-22.1 score points change",
        "period": "September–December 2024",
        "provenance": "system_derived",
    }
    row["text"] = (
        "From September to December 2024, the influence score went "
        "from -39.9 to -62.0."
    )
    assert _source_bundle_from_result(payload, row, "full_document") is not None

    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "4.5 score points increase",
        "period": "2023–2024",
        "provenance": "system_derived",
    }
    row["text"] = "Scores were 71.8 in 2024, improving on 67.3 in 2023."
    assert _source_bundle_from_result(payload, row, "full_document") is not None

    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "6 cases increase; 3 cases decrease",
        "period": "October 29, 2024",
        "provenance": "system_derived",
    }
    row["text"] = (
        "On October 29, 2024, group A increased from 10 cases to 16 cases; "
        "group B decreased from 8 cases to 5 cases."
    )
    assert _source_bundle_from_result(payload, row, "full_document") is not None

    payload["evidence_anchors"][0]["quantitative_result"]["estimate"] = (
        "6 cases increase and 3 cases decrease"
    )
    assert _source_bundle_from_result(payload, row, "full_document") is not None

    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "9 percentage points change at follow-up",
        "period": "October 29, 2024",
        "provenance": "system_derived",
    }
    row["text"] = (
        "On October 29, 2024, the rate fell from 40% to 31% at follow-up."
    )
    assert _source_bundle_from_result(payload, row, "full_document") is not None

    for estimate, source_text in (
        (
            "200 respondents increase",
            "Participants increased from 1800 respondents to 2000 respondents.",
        ),
        (
            "500 cases increase",
            "The count increased from 1499 cases to 1999 cases.",
        ),
        (
            "3 total cases",
            "The 2024 report found one case and two cases.",
        ),
    ):
        payload["evidence_anchors"][0]["quantitative_result"] = {
            "estimate": estimate,
            "provenance": "system_derived",
        }
        row["text"] = source_text
        assert _source_bundle_from_result(payload, row, "full_document") is not None

    for estimate, source_text in (
        ("5 cases increase", "Cases rose from 10 to 15 cases."),
        ("9 cases decrease", "Cases fell sharply from 40 to 31."),
        (
            "9 cases decrease",
            "Cases in five countries fell from 40 to 31.",
        ),
        ("50% relative increase", "Cases rose from 10 to 15 cases."),
        ("9 percentage points increase", "The response rate grew from 31% to 40%."),
        ("9 percentage points increase", "The response rate increased to 40% from 31%."),
    ):
        payload["evidence_anchors"][0]["quantitative_result"] = {
            "estimate": estimate,
            "provenance": "system_derived",
        }
        row["text"] = source_text
        assert _source_bundle_from_result(payload, row, "full_document") is not None


def test_quantitative_provenance_rejects_arithmetic_coincidence() -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "6%",
        "period": "October 29, 2024",
        "provenance": "system_derived",
    }

    with pytest.raises(
        SourceBundleQuantitativeProvenanceError,
        match="derived_estimate_not_supported_by_source_inputs",
    ):
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": (
                    "On October 29, 2024, there were 2 cases in 3 countries."
                ),
            },
            "full_document",
        )


@pytest.mark.parametrize(
    ("estimate", "text"),
    [
        (
            "22.5% relative decrease",
            "The measure changed from 40 countries to 31 years.",
        ),
        (
            "22.5% relative decrease",
            "Cases fell from 40 to 31 years.",
        ),
        ("2 cases per year", "There were 10 cases among 5 groups."),
        ("2 groups per case", "There were 10 cases among 5 groups."),
        ("12 total groups", "There were 3 groups with 4 cases each."),
        (
            "9 countries decrease",
            "Participants came from 40 countries and were assigned to 31 clinics.",
        ),
        (
            "9 countries decrease",
            "There were 9 countries\nThe rate fell from 40% to 31%.",
        ),
        (
            "9 countries decrease",
            "Cases fell from 40 to 31 in five countries.",
        ),
        (
            "9 percentage points increase",
            "Cases fell, but the response rate ranged from 31% to 40%.",
        ),
        (
            "9 percentage points increase",
            "Cases fell although the response rate ranged from 31% to 40%.",
        ),
        ("5 cases decrease", "Cases dropped to 5 cases from 20 cases."),
        (
            "9 countries increase",
            "There were 40 countries, an increase from 31 years.",
        ),
        (
            "9 cities decrease",
            "Cases declined; delegates traveled from 40 cities to 31 cities.",
        ),
        ("9 cases decrease", "Cases rose from 40 cases to 31 cases."),
        ("5 cases decrease", "The lower 5 cases were excluded."),
        ("5 cases decrease", "The decrease affected 5 cases."),
        ("5 cases decrease", "The count decreased among 5 cases."),
        ("5 cases decrease", "The count declined during 5 cases."),
        (
            "5 cases increase",
            "The interval ranged from 10 to 15 cases while another metric increased.",
        ),
        (
            "50% relative increase",
            "Cases increased from 10; controls increased to 15.",
        ),
        (
            "50% relative increase",
            "Cases increased from 10 and controls increased to 15.",
        ),
        (
            "50% relative increase",
            "Cases increased from 10, and controls increased to 15.",
        ),
        (
            "9 cases decrease",
            "Cases fell from 40; controls fell to 31.",
        ),
        ("10 total groups", "Group A had 5 cases and group B had 5 cases."),
    ],
)
def test_quantitative_provenance_rejects_dimensionally_invalid_derivations(
    estimate, text
) -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": estimate,
        "provenance": "system_derived",
    }

    with pytest.raises(
        SourceBundleQuantitativeProvenanceError,
        match="derived_estimate_not_supported_by_source_inputs",
    ):
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": text,
            },
            "full_document",
        )


@pytest.mark.parametrize(
    ("estimate", "text"),
    [
        ("5 cases down", "Five countries were sampled; cases rose from 10 to 15."),
        ("5 cases drop", "Five countries were sampled; cases rose from 10 to 15."),
        ("5 cases decline", "Five countries were sampled; cases rose from 10 to 15."),
        ("5 cases fall", "Five countries were sampled; cases rose from 10 to 15."),
        ("5 cases loss", "Five countries were sampled; cases rose from 10 to 15."),
        ("5 cases higher", "Five countries were sampled; cases fell from 15 to 10."),
        ("5 cases rise", "Five countries were sampled; cases fell from 15 to 10."),
        ("5 cases rose", "Five countries were sampled; cases fell from 15 to 10."),
    ],
)
def test_quantitative_provenance_direction_words_cannot_use_literal_coincidence(
    estimate, text
) -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": estimate,
        "provenance": "system_derived",
    }

    with pytest.raises(SourceBundleQuantitativeProvenanceError):
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": text,
            },
            "full_document",
        )


def test_quantitative_formula_ignores_period_prefix_numbers() -> None:
    payload = _bundle_payload()
    result = {
        "estimate": "9 cases decrease",
        "provenance": "system_derived",
    }
    payload["evidence_anchors"][0]["quantitative_result"] = result
    row = {
        "source_id": "source-zotero-A1",
        "zotero_item_key": "A1",
        "text": (
            "Cases decreased from Q4 2020 to Q1 2021, with the count "
            "moving to 31 from 40."
        ),
    }
    assert _source_bundle_from_result(payload, row, "full_document") is not None

    for estimate in ("3 cases decrease", "39 cases decrease"):
        result["estimate"] = estimate
        with pytest.raises(SourceBundleQuantitativeProvenanceError):
            _source_bundle_from_result(payload, row, "full_document")

    result["estimate"] = "9 cases decrease"
    for prefix in (
        "From 2023 through 2024",
        "From 2024 onward",
        "From the 2024 reporting period onward",
        "From a sample of 2024 respondents",
        "From 2024 records",
    ):
        row["text"] = f"{prefix}, cases fell from 40 to 31."
        assert _source_bundle_from_result(payload, row, "full_document") is not None


@pytest.mark.parametrize("provenance", ["source_reported", "unknown"])
def test_reported_quantitative_direction_must_match_source(provenance) -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "31% increase",
        "provenance": provenance,
    }

    with pytest.raises(
        SourceBundleQuantitativeProvenanceError,
        match="reported_estimate_not_found_in_source",
    ):
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": "The rate fell to 31%.",
            },
            "full_document",
        )

    with pytest.raises(SourceBundleQuantitativeProvenanceError):
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": "Output increased while the response rate remained 31%.",
            },
            "full_document",
        )

    payload["evidence_anchors"][0]["quantitative_result"]["estimate"] = (
        "40% increase"
    )
    assert (
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": "The response rate grew from 31% to 40%.",
            },
            "full_document",
        )
        is not None
    )


def test_derived_direction_cannot_cross_contrasting_clause() -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "9 percentage points increase",
        "provenance": "system_derived",
    }

    with pytest.raises(
        SourceBundleQuantitativeProvenanceError,
        match="derived_estimate_not_supported_by_source_inputs",
    ):
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": (
                    "Cases increased, while the approval interval ranged "
                    "from 31% to 40%."
                ),
            },
            "full_document",
        )

    payload["evidence_anchors"][0]["quantitative_result"]["estimate"] = (
        "50% relative increase"
    )
    with pytest.raises(
        SourceBundleQuantitativeProvenanceError,
        match="derived_estimate_not_supported_by_source_inputs",
    ):
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": (
                    "Response gain—the percentage of respondents who agreed—"
                    "rose elsewhere, but ranged from 10 to 15."
                ),
            },
            "full_document",
        )


def test_quantitative_provenance_rejects_one_supported_and_one_invented_value() -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "43,824 deaths; 999,999 injured",
        "period": "October 29, 2024",
        "provenance": "source_reported",
    }
    with pytest.raises(
        SourceBundleQuantitativeProvenanceError,
        match="reported_estimate_not_found_in_source",
    ):
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": "On October 29, 2024, the source reported 43,824 deaths.",
            },
            "full_document",
        )


@pytest.mark.parametrize(
    ("period", "text", "reason"),
    [
        (
            "October 29, 2024",
            "As of October 29:\nDeaths: 43,824.",
            "period_date_not_found_in_source",
        ),
        (
            "October 29, 2024",
            "Deaths: 43,824.\n"
            + ("Unrelated context.\n" * 100)
            + "A separate hearing occurred on October 29, 2024.",
            "period_date_not_local_to_reported_estimate",
        ),
        (
            "October 1–29, 2024",
            "On October 29, 2024, deaths reached 43,824.",
            "period_date_not_found_in_source",
        ),
        (
            "October 1 to 29, 2024",
            "On October 1, 2024, deaths reached 43,824.",
            "period_date_not_found_in_source",
        ),
        (
            "1–29 October 2024",
            "On October 29, 2024, deaths reached 43,824.",
            "period_date_not_found_in_source",
        ),
        (
            "October 1 to October 29, 2024",
            "On October 1, deaths reached 43,824; the report was silent on October 29.",
            "period_date_not_found_in_source",
        ),
    ],
)
def test_quantitative_provenance_rejects_unsupported_date_scope(
    period, text, reason
) -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "43,824",
        "period": period,
        "provenance": "source_reported",
    }
    with pytest.raises(SourceBundleQuantitativeProvenanceError, match=reason):
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": text,
            },
            "full_document",
        )


def test_quantitative_provenance_rejects_backward_and_unbounded_date_scope() -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "43,824",
        "period": "October 29, 2024",
        "provenance": "source_reported",
    }
    for text in (
        "Deaths: 43,824.\nContext.\nFigures as of October 29, 2024:",
        "Table totals as of October 29, 2024:\n"
        + "".join(f"Category {index}\n" for index in range(100))
        + "Deaths: 43,824.",
    ):
        with pytest.raises(
            SourceBundleQuantitativeProvenanceError,
            match="period_date_not_local_to_reported_estimate",
        ):
            _source_bundle_from_result(
                payload,
                {
                    "source_id": "source-zotero-A1",
                    "zotero_item_key": "A1",
                    "text": text,
                },
                "full_document",
            )


def test_quantitative_provenance_checks_undated_estimates() -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "999,999",
        "period": "2024",
        "provenance": "source_reported",
    }

    with pytest.raises(
        SourceBundleQuantitativeProvenanceError,
        match="reported_estimate_not_found_in_source",
    ):
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": "In 2024 no numerical result was reported.",
            },
            "full_document",
        )

    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "5 cases",
        "period": "2023–2099",
        "provenance": "source_reported",
    }
    with pytest.raises(
        SourceBundleQuantitativeProvenanceError,
        match="period_date_not_found_in_source",
    ):
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": "The 2023 report recorded 5 cases.",
            },
            "full_document",
        )


def test_quantitative_provenance_checks_auxiliary_numeric_fields() -> None:
    for field in (
        "estimand_type",
        "model",
        "outcome_definition",
        "population",
        "sample",
        "scale",
        "statistic",
        "unit",
    ):
        payload = _bundle_payload()
        payload["evidence_anchors"][0]["quantitative_result"] = {
            "estimate": "43,061",
            "period": "October 29, 2024",
            "provenance": "source_reported",
            field: "999,999 people",
        }

        with pytest.raises(
            SourceBundleQuantitativeProvenanceError,
            match="reported_estimate_not_found_in_source",
        ):
            _source_bundle_from_result(
                payload,
                {
                    "source_id": "source-zotero-A1",
                    "zotero_item_key": "A1",
                    "text": "On October 29, 2024, the source reported 43,061 deaths.",
                },
                "full_document",
            )

    for result, source_text in (
        (
            {
                "estimate": "43,061 deaths",
                "sample": "100,000 residents",
                "provenance": "source_reported",
            },
            "The result was 43,061 deaths.\nThe sample included 100,000 residents.",
        ),
        (
            {
                "estimate": "9 percentage points decrease",
                "baseline": "40%",
                "provenance": "system_derived",
            },
            "The reported rate fell from 40% to 31%.",
        ),
        (
            {
                "estimate": "-22.7 percentage points",
                "baseline": "12.2",
                "provenance": "system_derived",
            },
            (
                "Net favorability—the percentage viewing a country positively "
                "after subtracting the percentage viewing it negatively.\n"
                "Net favorability was dropping\n"
                "from 12.2 to -10.5 over the same period."
            ),
        ),
    ):
        payload = _bundle_payload()
        payload["evidence_anchors"][0]["quantitative_result"] = result
        assert (
            _source_bundle_from_result(
                payload,
                {
                    "source_id": "source-zotero-A1",
                    "zotero_item_key": "A1",
                    "text": source_text,
                },
                "full_document",
            )
            is not None
        )

    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "43,061",
        "baseline": "40,000 on October 1, 2024",
        "period": "October 29, 2024",
        "provenance": "source_reported",
    }
    assert (
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": (
                    "On October 1, 2024, the baseline was 40,000.\n"
                    "On October 29, 2024, the result was 43,061."
                ),
            },
            "full_document",
        )
        is not None
    )

    for source_text, baseline in (
        (
            "The result was 43,061.\n"
            + ("Unrelated context.\n" * 100)
            + "A different study sampled 999 participants.",
            None,
        ),
        (
            "On October 1, 2024, a hearing occurred.\n"
            + ("Unrelated context.\n" * 100)
            + "The comparison group had 40 cases.\nThe result was 43,061.",
            "40 cases on October 1, 2024",
        ),
        (
            "The result was 43,061.\n"
            + ("Unrelated context.\n" * 100)
            + "On October 1, 2024, a different study had 40 cases.",
            "40 cases on October 1, 2024",
        ),
    ):
        payload = _bundle_payload()
        payload["evidence_anchors"][0]["quantitative_result"] = {
            "estimate": "43,061",
            "provenance": "source_reported",
            **(
                {"baseline": baseline}
                if baseline is not None
                else {"sample": "999 participants"}
            ),
        }
        with pytest.raises(SourceBundleQuantitativeProvenanceError):
            _source_bundle_from_result(
                payload,
                {
                    "source_id": "source-zotero-A1",
                    "zotero_item_key": "A1",
                    "text": source_text,
                },
                "full_document",
            )


def test_quantitative_provenance_accepts_qualified_year() -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "early 2002",
        "period": "early 2002",
        "provenance": "source_reported",
    }

    assert (
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": "The program was created in early 2002.",
            },
            "full_document",
        )
        is not None
    )


def test_quantitative_provenance_accepts_percentage_baseline_comparison() -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "additional ten percent",
        "baseline": "45 percent versus 65 percent expected proportion of the vote",
        "provenance": "source_reported",
    }

    assert (
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": (
                    "The expected vote for nonincumbents was 45 percent vs. 65 percent, "
                    "but incumbency could still add an additional ten percent."
                ),
            },
            "full_document",
        )
        is not None
    )


def test_quantitative_provenance_ignores_hyphenated_label_numbers() -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "first case",
        "outcome_definition": "First confirmed TEST-19 case within Zone Alpha",
        "period": "17 April 2098",
        "provenance": "source_reported",
    }

    assert (
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": (
                    "On 17 April 2098, the lab confirmed its first case of "
                    "TEST-19 within Zone Alpha."
                ),
            },
            "full_document",
        )
        is not None
    )


def test_quantitative_provenance_auxiliary_uses_discriminating_anchor() -> None:
    cases = (
        (
            {
                "estimate": "43,061 deaths; 1 region",
                "sample": "999 participants",
                "provenance": "source_reported",
            },
            "The result was 43,061 deaths in 1 region.\n"
            + ("Unrelated context.\n" * 100)
            + "A different study had 999 participants in 1 region.",
        ),
        (
            {
                "estimate": "9 percentage points decrease",
                "sample": "999 participants",
                "provenance": "system_derived",
            },
            "The reported rate fell from 40% to 31%.\n"
            + ("Unrelated context.\n" * 100)
            + "A different study had 999 participants in 9 countries.",
        ),
        (
            {
                "estimate": "42 cases",
                "sample": "999 participants",
                "period": "October 29, 2024",
                "provenance": "source_reported",
            },
            "On October 29, 2024, the result was 42 cases.\n"
            + ("Unrelated context.\n" * 100)
            + "A different study had 42 cases among 999 participants.",
        ),
    )
    for result, source_text in cases:
        payload = _bundle_payload()
        payload["evidence_anchors"][0]["quantitative_result"] = result
        with pytest.raises(SourceBundleQuantitativeProvenanceError):
            _source_bundle_from_result(
                payload,
                {
                    "source_id": "source-zotero-A1",
                    "zotero_item_key": "A1",
                    "text": source_text,
                },
                "full_document",
            )


def test_quantitative_provenance_auxiliary_disambiguates_repeated_estimate() -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "12",
        "baseline": "30,000",
        "period": "October 7",
        "provenance": "source_reported",
    }
    text = (
        "On October 7, another 12 events affected 20 people.\n"
        + ("Context without quantities.\n" * 20)
        + "The source accused 12 of its 30,000 employees of participating "
        "in the October 7 attacks."
    )

    assert (
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": text,
            },
            "full_document",
        )
        is not None
    )


def test_quantitative_provenance_accepts_rephrased_reporting_qualifier() -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "three cases",
        "comparison_group": "8 initially reported",
        "provenance": "source_reported",
    }

    assert (
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": "The audit found three cases, not 8 as originally reported.",
            },
            "full_document",
        )
        is not None
    )

    payload["evidence_anchors"][0]["quantitative_result"][
        "comparison_group"
    ] = "8 initially reported cases"
    with pytest.raises(SourceBundleQuantitativeProvenanceError):
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": "The audit found three cases, not 8 as originally reported people.",
            },
            "full_document",
        )


def test_quantitative_provenance_accepts_hyphenated_people_count() -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "10 percent",
        "sample": "12,000 local staff",
        "provenance": "source_reported",
    }

    assert (
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": (
                    "The review reported that 10 percent of the organization's "
                    "12,000-person local staff met the criterion."
                ),
            },
            "full_document",
        )
        is not None
    )

    payload["evidence_anchors"][0]["quantitative_result"]["sample"] = (
        "12,000 local facilities"
    )
    with pytest.raises(SourceBundleQuantitativeProvenanceError):
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": (
                    "The review reported that 10 percent of the organization's "
                    "12,000-person local staff met the criterion."
                ),
            },
            "full_document",
        )


@pytest.mark.parametrize(
    ("field", "value", "text"),
    [
        (
            "sample",
            "200 participants",
            "Study A reported 42 cases among 100 participants; "
            "study B reported 43 cases among 200 participants.",
        ),
        (
            "population",
            "adults in 2 countries",
            "Children in 1 country had 42 cases; "
            "adults in 2 countries had 43 cases.",
        ),
        (
            "sample",
            "200 participants",
            "Study A reported 42 cases among 100 participants and "
            "study B reported 43 cases among 200 participants.",
        ),
        (
            "population",
            "adults in 2 countries",
            "Children in 1 country had 42 cases and "
            "adults in 2 countries had 43 cases.",
        ),
    ],
)
def test_quantitative_auxiliary_cannot_cross_compound_findings(
    field, value, text
) -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "42 cases",
        "provenance": "source_reported",
        field: value,
    }

    with pytest.raises(SourceBundleQuantitativeProvenanceError):
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": text,
            },
            "full_document",
        )


def test_quantitative_provenance_does_not_treat_markdown_as_footnote() -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "10 controls; 15 participants",
        "provenance": "source_reported",
    }
    row = {
        "source_id": "source-zotero-A1",
        "zotero_item_key": "A1",
        "text": (
            "10 controls.\n"
            "15 participants and *42 outcomes*\n"
            "* Based on prior work, limitations apply."
        ),
    }

    assert _source_bundle_from_result(payload, row, "full_document") is not None

    row["text"] = (
        "10 controls.\n"
        "*15 participants and 42 outcomes*\n"
        "*Based on prior work*"
    )
    assert _source_bundle_from_result(payload, row, "full_document") is not None

    row["text"] = (
        "10 controls.\n"
        "15 participants.\n"
        "**42 outcomes**\n"
        "* Based on the registered protocol."
    )
    assert _source_bundle_from_result(payload, row, "full_document") is not None

    row["text"] = (
        "10 controls.\n"
        "15 participants.\n"
        "2 * 3 = 6 calculation.\n"
        "* Based on the registered protocol."
    )
    assert _source_bundle_from_result(payload, row, "full_document") is not None


def test_quantitative_provenance_detects_repeated_asterisk_footnotes() -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "42 cases",
        "provenance": "source_reported",
    }

    with pytest.raises(
        SourceBundleQuantitativeProvenanceError,
        match="footnote_scope_combines_marked_and_unmarked_quantities",
    ):
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": "42 cases*; 15 controls*.\n* Based on the entire war.",
            },
            "full_document",
        )


@pytest.mark.parametrize(
    "scope",
    [
        "entire war",
        "whole war",
        "full conflict",
        "war to date",
        "days 7 to 12",
        "January through March 2024",
    ],
)
def test_quantitative_provenance_requires_complete_footnote_period(scope) -> None:
    payload = _bundle_payload()
    result = {
        "estimate": "42 cases",
        "provenance": "source_reported",
    }
    payload["evidence_anchors"][0]["quantitative_result"] = result
    row = {
        "source_id": "source-zotero-A1",
        "zotero_item_key": "A1",
        "text": f"42 cases*.\n* Based on the {scope}.",
    }

    with pytest.raises(
        SourceBundleQuantitativeProvenanceError,
        match="footnote_scope_combines_marked_and_unmarked_quantities",
    ):
        _source_bundle_from_result(payload, row, "full_document")

    result["period"] = scope
    assert _source_bundle_from_result(payload, row, "full_document") is not None


def test_quantitative_provenance_leaves_attribution_footnotes_unscoped() -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "42 cases",
        "provenance": "source_reported",
    }

    assert (
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": "42 cases*.\n* Source: Ministry of Health.",
            },
            "full_document",
        )
        is not None
    )

def test_quantitative_provenance_rejects_unmarked_footnote_period() -> None:
    row = {
        "source_id": "source-zotero-A1",
        "zotero_item_key": "A1",
        "text": (
            "Every hour:\n\n15 people are killed.\n\n"
            "42 bombs are dropped*.\n\n"
            "* Based on the first six days of the war."
        ),
    }

    for period in (
        "First six days of the war",
        "During the first six days of this war",
        "Early first six days of the war",
        "Initial week of the war",
    ):
        payload = _bundle_payload()
        payload["evidence_anchors"][0]["quantitative_result"] = {
            "estimate": "15 killed per hour",
            "period": period,
            "provenance": "source_reported",
        }
        with pytest.raises(
            SourceBundleQuantitativeProvenanceError,
            match="footnote_scope_combines_marked_and_unmarked_quantities",
        ):
            _source_bundle_from_result(payload, row, "full_document")


def test_quantitative_provenance_rejects_duplicate_marked_footnote_token() -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "42 controls",
        "period": "First six days",
        "provenance": "source_reported",
    }

    with pytest.raises(
        SourceBundleQuantitativeProvenanceError,
        match="footnote_scope_combines_marked_and_unmarked_quantities",
    ):
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": (
                    "42 controls.\n42 outcomes*.\n"
                    "* Based on the first six days."
                ),
            },
            "full_document",
        )


def test_quantitative_provenance_requires_local_year_only_period() -> None:
    for period, year_line in (
        ("2024", "The 2024 report opened with background."),
        ("In 2024", "In 2024 the report opened with background."),
        ("FY2024", "FY2024 opened with background."),
        ("1890", "The 1890 report opened with background."),
    ):
        payload = _bundle_payload()
        payload["evidence_anchors"][0]["quantitative_result"] = {
            "estimate": "43,824 deaths",
            "period": period,
            "provenance": "source_reported",
        }
        with pytest.raises(SourceBundleQuantitativeProvenanceError):
            _source_bundle_from_result(
                payload,
                {
                    "source_id": "source-zotero-A1",
                    "zotero_item_key": "A1",
                    "text": (
                        f"{year_line}\n"
                        + ("Unrelated prose.\n" * 100)
                        + "The source reported 43,824 deaths."
                    ),
                },
                "full_document",
            )


@pytest.mark.parametrize(
    ("period", "local_text"),
    [
        ("October 2024", "As of October 2024, 43 cases were reported."),
        ("October of 2024", "As of October 2024, 43 cases were reported."),
        ("Q1 2024", "In Q1 2024, 43 cases were reported."),
        (
            "First quarter 2024",
            "In the First quarter 2024, 43 cases were reported.",
        ),
        (
            "First quarter of 2024",
            "In the First quarter 2024, 43 cases were reported.",
        ),
        ("Q1–Q2 2024", "During Q1 through Q2 2024, 43 cases were reported."),
        (
            "First quarter through second quarter of 2024",
            "During first quarter–second quarter 2024, 43 cases were reported.",
        ),
        ("Spring 2024", "During Spring 2024, 43 cases were reported."),
        (
            "Spring through Summer 2024",
            "During Spring–Summer 2024, 43 cases were reported.",
        ),
        ("FY2024", "During fiscal year 2024, 43 cases were reported."),
        (
            "Calendar year 2024",
            "During calendar year 2024, 43 cases were reported.",
        ),
    ],
)
def test_quantitative_provenance_binds_named_periods_locally(
    period, local_text
) -> None:
    payload = _bundle_payload()
    result = {
        "estimate": "43 cases",
        "period": period,
        "provenance": "source_reported",
    }
    payload["evidence_anchors"][0]["quantitative_result"] = result
    row = {
        "source_id": "source-zotero-A1",
        "zotero_item_key": "A1",
        "text": local_text,
    }
    assert _source_bundle_from_result(payload, row, "full_document") is not None

    result["estimate"] = "42 cases"
    row["text"] = (
        "The 2024 report begins with 42 cases.\n"
        + ("Unrelated prose.\n" * 20)
        + local_text
    )
    with pytest.raises(SourceBundleQuantitativeProvenanceError):
        _source_bundle_from_result(payload, row, "full_document")


def test_quantitative_provenance_requires_every_named_period_endpoint() -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "43 cases",
        "period": "January through March 2024",
        "provenance": "source_reported",
    }
    row = {
        "source_id": "source-zotero-A1",
        "zotero_item_key": "A1",
        "text": "From January through March 2024, 43 cases were reported.",
    }
    assert _source_bundle_from_result(payload, row, "full_document") is not None

    row["text"] = "In January 2024, 43 cases were reported."
    with pytest.raises(
        SourceBundleQuantitativeProvenanceError,
        match="period_date_not_found_in_source",
    ):
        _source_bundle_from_result(payload, row, "full_document")


@pytest.mark.parametrize(
    ("period", "wrong_local", "exact_remote"),
    [
        ("Q1 through Q2 2024", "Q3 through Q2 2024", "Q1–Q2 2024"),
        (
            "First quarter through second quarter 2024",
            "Third quarter through second quarter 2024",
            "First quarter–second quarter 2024",
        ),
        (
            "Spring through Summer 2024",
            "Fall through Summer 2024",
            "Spring–Summer 2024",
        ),
    ],
)
def test_quantitative_provenance_rejects_wrong_named_period_endpoint(
    period, wrong_local, exact_remote
) -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "43 cases",
        "period": period,
        "provenance": "source_reported",
    }
    row = {
        "source_id": "source-zotero-A1",
        "zotero_item_key": "A1",
        "text": (
            f"During {wrong_local}, 43 cases were reported.\n"
            + ("Unrelated prose.\n" * 20)
            + f"During {exact_remote}, 99 cases were reported."
        ),
    }

    with pytest.raises(SourceBundleQuantitativeProvenanceError):
        _source_bundle_from_result(payload, row, "full_document")


def test_quantitative_provenance_distinguishes_year_kinds() -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "43 cases",
        "period": "FY 2024",
        "provenance": "source_reported",
    }
    row = {
        "source_id": "source-zotero-A1",
        "zotero_item_key": "A1",
        "text": (
            "During calendar year 2024, 43 cases were reported.\n"
            + ("Unrelated prose.\n" * 20)
            + "During fiscal year 2024, 99 cases were reported."
        ),
    }

    with pytest.raises(SourceBundleQuantitativeProvenanceError):
        _source_bundle_from_result(payload, row, "full_document")


@pytest.mark.parametrize(
    ("period", "text"),
    [
        (
            "October 29, 2024",
            "On October 29, 2023, 42 cases were reported; "
            "on October 29, 2024, 43 cases were reported.",
        ),
        (
            "October 2024",
            "In October 2023, 42 cases were reported; "
            "in October 2024, 43 cases were reported.",
        ),
        (
            "2024",
            "In 2023, 42 cases were reported; in 2024, 43 cases were reported.",
        ),
        (
            "Q2 2024",
            "Q1 2024 had 42 cases; Q2 2024 had 43 cases.",
        ),
        (
            "2024",
            "In 2023, 42 cases were reported and in 2024, 43 cases were reported.",
        ),
        (
            "October 2024",
            "October 2023 had 42 cases and October 2024 had 43 cases.",
        ),
        (
            "Q2 2024",
            "Q1 2024 had 42 cases and Q2 2024 had 43 cases.",
        ),
        (
            "2024",
            "The total rose from 42 cases in 2023 to 43 cases in 2024.",
        ),
    ],
)
def test_quantitative_provenance_binds_period_within_compound_line(
    period, text
) -> None:
    payload = _bundle_payload()
    result = {
        "estimate": "42 cases",
        "period": period,
        "provenance": "source_reported",
    }
    payload["evidence_anchors"][0]["quantitative_result"] = result
    row = {
        "source_id": "source-zotero-A1",
        "zotero_item_key": "A1",
        "text": text,
    }

    with pytest.raises(SourceBundleQuantitativeProvenanceError):
        _source_bundle_from_result(payload, row, "full_document")

    result["estimate"] = "43 cases"
    assert _source_bundle_from_result(payload, row, "full_document") is not None


@pytest.mark.parametrize(
    ("period", "text"),
    [
        ("2024", "In 2024, results follow:\nIn 2023, 42 cases were reported."),
        (
            "October 2024",
            "October 2024 results:\nIn November 2024, 42 cases were reported.",
        ),
        (
            "Q2 2024",
            "Q2 2024 results:\nIn Q1 2024, 42 cases were reported.",
        ),
        (
            "October 29, 2024",
            "On October 29, 2024, results follow:\n"
            "42 cases were counted on October 1, 2024.",
        ),
    ],
)
def test_quantitative_provenance_does_not_override_explicit_value_period(
    period, text
) -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "42 cases",
        "period": period,
        "provenance": "source_reported",
    }

    with pytest.raises(SourceBundleQuantitativeProvenanceError):
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": text,
            },
            "full_document",
        )


@pytest.mark.parametrize(
    "period",
    [
        "from 2023 to 2024",
        "2023 and 2024",
        "2023/2024",
        "since 2023",
        "through 2024",
    ],
)
def test_quantitative_provenance_binds_all_explicit_year_periods_locally(
    period,
) -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "42 cases",
        "period": period,
        "provenance": "source_reported",
    }
    row = {
        "source_id": "source-zotero-A1",
        "zotero_item_key": "A1",
        "text": (
            "42 cases were mentioned in the introduction.\n"
            + ("Unrelated prose.\n" * 20)
            + f"During {period}, 43 cases were reported."
        ),
    }

    with pytest.raises(SourceBundleQuantitativeProvenanceError):
        _source_bundle_from_result(payload, row, "full_document")


@pytest.mark.parametrize(
    "noun", ["study", "survey", "cohort", "election", "wave"]
)
def test_quantitative_provenance_recognizes_year_modified_source_periods(
    noun,
) -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "42 cases",
        "period": "2023",
        "provenance": "source_reported",
    }

    assert (
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": f"The 2023 {noun} reported 42 cases.",
            },
            "full_document",
        )
        is not None
    )


def test_quantitative_provenance_understands_fiscal_between_and_season_years() -> None:
    payload = _bundle_payload()
    result = {
        "estimate": "9 cases",
        "period": "FY 2024-25",
        "provenance": "source_reported",
    }
    payload["evidence_anchors"][0]["quantitative_result"] = result
    with pytest.raises(SourceBundleQuantitativeProvenanceError):
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": (
                    "FY 2024-25 opened with background.\n"
                    + ("Unrelated prose.\n" * 100)
                    + "The source reported 9 cases."
                ),
            },
            "full_document",
        )


@pytest.mark.parametrize(
    ("period", "text"),
    [
        (
            "calendar year 2024",
            "The calendar year 2024 opened with background.\n"
            + ("Unrelated prose.\n" * 100)
            + "The source reported 9 cases.",
        ),
        (
            "2023 through December 31, 2024",
            "The 2023 report opened with background.\n"
            + ("Unrelated prose.\n" * 100)
            + "On December 31, 2024, the source reported 9 cases.",
        ),
        (
            "2024",
            "The 2024 report opened with background.\n"
            "Updated: October 29, 2024\n"
            "The source reported 9 cases.",
        ),
    ],
)
def test_quantitative_provenance_rejects_nonlocal_descriptive_years(
    period, text
) -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "9 cases",
        "period": period,
        "provenance": "source_reported",
    }
    with pytest.raises(SourceBundleQuantitativeProvenanceError):
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": text,
            },
            "full_document",
        )


def test_quantitative_provenance_binds_coordinated_and_wrapped_dates() -> None:
    payload = _bundle_payload()
    result = {
        "estimate": "9 cases",
        "period": "October 1 and November 2, 2024",
        "provenance": "source_reported",
    }
    payload["evidence_anchors"][0]["quantitative_result"] = result
    with pytest.raises(SourceBundleQuantitativeProvenanceError):
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": (
                    "On October 1, 2023 and November 2, 2024, "
                    "the source reported 9 cases."
                ),
            },
            "full_document",
        )

    result["period"] = "October 29, 2024"
    assert (
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": "The source reported 9 cases\non October 29, 2024.",
            },
            "full_document",
        )
        is not None
    )

    for period, text in (
        ("Between 2023 and 2024", "Between 2023 and 2024, 9 cases were reported."),
        ("Spring 2024", "In Spring 2024, 9 cases were reported."),
    ):
        result["period"] = period
        assert (
            _source_bundle_from_result(
                payload,
                {
                    "source_id": "source-zotero-A1",
                    "zotero_item_key": "A1",
                    "text": text,
                },
                "full_document",
            )
            is not None
        )


def test_quantitative_auxiliary_values_cannot_bind_to_remote_duplicate_claim() -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "40 of 100 respondents",
        "period": "October 29, 2024",
        "provenance": "source_reported",
        "sample": "999 adults",
    }

    with pytest.raises(
        SourceBundleQuantitativeProvenanceError,
        match="reported_estimate_not_found_in_source",
    ):
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": (
                    "On October 29, 2024, 40 of 100 respondents answered yes.\n"
                    "The sample included 200 adults.\n"
                    + ("Unrelated prose.\n" * 100)
                    + "On October 29, 2024, 40 of 100 respondents answered yes.\n"
                    "The sample included 999 adults."
                ),
            },
            "full_document",
        )

    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "42 cases",
        "period": "October 29, 2024",
        "provenance": "source_reported",
        "sample": "100 participants",
    }
    assert (
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": (
                    "On October 29, 2024, 42 cases occurred among 100 participants.\n"
                    + ("Unrelated prose.\n" * 100)
                    + "On October 29, 2024, a later passage repeated 42 cases."
                ),
            },
            "full_document",
        )
        is not None
    )


def test_quantitative_provenance_requires_local_slash_date() -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "43,824 deaths",
        "period": "October 29, 2024",
        "provenance": "source_reported",
    }
    with pytest.raises(
        SourceBundleQuantitativeProvenanceError,
        match="period_date_not_local_to_reported_estimate",
    ):
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": (
                    "Background dated 10/29/2024.\n"
                    + ("Unrelated prose.\n" * 100)
                    + "The source reported 43,824 deaths."
                ),
            },
            "full_document",
        )


def test_quantitative_provenance_does_not_treat_quantity_as_period_year() -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "5 cases",
        "period": "2024",
        "provenance": "source_reported",
    }
    with pytest.raises(
        SourceBundleQuantitativeProvenanceError,
        match="period_date_not_found_in_source",
    ):
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": "The study included 2024 respondents and reported 5 cases.",
            },
            "full_document",
        )


def test_quantitative_provenance_accepts_bounded_year_column_table() -> None:
    payload = _bundle_payload()
    cells = [
        "Show all years",
        "Expand All",
        "2024",
        "2023",
        "2022",
        "2020",
        "2019",
        "Pillar",
        "Score",
        "Rank",
        "Score",
        "Rank",
        "Score",
        "Rank",
        "Score",
        "Rank",
        "Score",
        "Rank",
        "Global Soft Power Index",
        "48.7",
        "+0.3",
        "32",
        "-5",
        "48.4",
        "+0.8",
        "27",
        "-4",
        "47.6",
        "-",
        "23",
        "-",
        "-",
        "43.6",
        "+1.0",
        "25",
        "=",
        "42.6",
        "25",
        "-",
        "Familiarity",
        "6.9",
        "+0.5",
        "25",
        "+1",
        "6.4",
        "-0.1",
        "26",
        "-2",
        "6.5",
        "-",
        "24",
        "-",
        "-",
        "5.9",
        "-0.1",
        "25",
        "+2",
        "6.0",
        "27",
    ]
    row = {
        "source_id": "source-zotero-A1",
        "zotero_item_key": "A1",
        "text": "\n\n".join(cells),
    }

    single_result = {
        "statistic": "Score",
        "outcome_definition": "Global Soft Power Index score",
        "estimate": "48.7",
        "period": "2024",
        "provenance": "source_reported",
    }
    payload["evidence_anchors"][0]["quantitative_result"] = single_result
    assert _source_bundle_from_result(payload, row, "full_document") is not None
    for period in ("", "2023"):
        single_result["period"] = period
        with pytest.raises(SourceBundleQuantitativeProvenanceError):
            _source_bundle_from_result(payload, row, "full_document")

    result = {
        "estimate": "Score 48.7; rank 32",
        "period": "2024",
        "provenance": "source_reported",
    }
    payload["evidence_anchors"][0]["quantitative_result"] = result
    assert _source_bundle_from_result(payload, row, "full_document") is not None

    for outcome_definition in (
        "Familiarity",
        "Familiarity score",
        "Familiarity metric",
    ):
        result["outcome_definition"] = outcome_definition
        with pytest.raises(SourceBundleQuantitativeProvenanceError):
            _source_bundle_from_result(payload, row, "full_document")
    result["outcome_definition"] = "Global Soft Power Index score"
    assert _source_bundle_from_result(payload, row, "full_document") is not None
    result["outcome_definition"] = "Global Soft Power Index and Familiarity"
    with pytest.raises(SourceBundleQuantitativeProvenanceError):
        _source_bundle_from_result(payload, row, "full_document")
    result.pop("outcome_definition")

    result["estimate"] = "48.7 score and 32 rank"
    assert _source_bundle_from_result(payload, row, "full_document") is not None

    result["estimate"] = "32 score and 48.7 rank"
    with pytest.raises(SourceBundleQuantitativeProvenanceError):
        _source_bundle_from_result(payload, row, "full_document")

    result["estimate"] = "Unknown metric score 48.7; rank 32"
    with pytest.raises(SourceBundleQuantitativeProvenanceError):
        _source_bundle_from_result(payload, row, "full_document")

    result["estimate"] = "score 32; ranking 48.7"
    with pytest.raises(SourceBundleQuantitativeProvenanceError):
        _source_bundle_from_result(payload, row, "full_document")

    result["estimate"] = "Score 48.4; rank 27"
    with pytest.raises(SourceBundleQuantitativeProvenanceError):
        _source_bundle_from_result(payload, row, "full_document")

    result["estimate"] = "Score 48.7; rank 25"
    with pytest.raises(SourceBundleQuantitativeProvenanceError):
        _source_bundle_from_result(payload, row, "full_document")

    result["estimate"] = (
        "score 32; rank 48.7; score change -5; rank change +0.3"
    )
    with pytest.raises(SourceBundleQuantitativeProvenanceError):
        _source_bundle_from_result(payload, row, "full_document")

    result["estimate"] = "score 32 and rank 48.7"
    with pytest.raises(SourceBundleQuantitativeProvenanceError):
        _source_bundle_from_result(payload, row, "full_document")

    result["estimate"] = (
        "Global Soft Power Index score 48.7; Familiarity rank 25"
    )
    with pytest.raises(SourceBundleQuantitativeProvenanceError):
        _source_bundle_from_result(payload, row, "full_document")

    result["estimate"] = "2024 respondents"
    with pytest.raises(SourceBundleQuantitativeProvenanceError):
        _source_bundle_from_result(payload, row, "full_document")

    for estimate in (
        "32 respondents",
        "48.7 respondents",
        "25 countries",
        "99 participants",
        "rank 2024",
    ):
        result["estimate"] = estimate
        with pytest.raises(SourceBundleQuantitativeProvenanceError):
            _source_bundle_from_result(payload, row, "full_document")

    result.update(estimate="Score 48.7; rank 32", sample="2024", period="2024")
    with pytest.raises(SourceBundleQuantitativeProvenanceError):
        _source_bundle_from_result(payload, row, "full_document")
    result.pop("sample")

    result["estimate"] = "2023 score 48.4; rank 27"
    with pytest.raises(SourceBundleQuantitativeProvenanceError):
        _source_bundle_from_result(payload, row, "full_document")

    result.update(estimate="Score 48.7; rank 32", sample="2024 respondents")
    with pytest.raises(SourceBundleQuantitativeProvenanceError):
        _source_bundle_from_result(payload, row, "full_document")
    result.pop("sample")

    for field in ("sample", "denominator", "population", "uncertainty"):
        result[field] = "32"
        with pytest.raises(SourceBundleQuantitativeProvenanceError):
            _source_bundle_from_result(payload, row, "full_document")
        result.pop(field)

    result.update(
        estimate="2024 score 48.7; rank 32; score change +0.3; rank change -5",
        baseline="2023 score 48.4; rank 27",
        period="2024 versus 2023",
    )
    assert _source_bundle_from_result(payload, row, "full_document") is not None

    result["estimate"] = (
        "2024 score 48.7; rank 32; score change +0.3; ranking change -5"
    )
    assert _source_bundle_from_result(payload, row, "full_document") is not None

    result["baseline"] = "2024 score 48.4; rank 27"
    with pytest.raises(SourceBundleQuantitativeProvenanceError):
        _source_bundle_from_result(payload, row, "full_document")

    result["baseline"] = "2023 score 6.4; rank 26"
    with pytest.raises(SourceBundleQuantitativeProvenanceError):
        _source_bundle_from_result(payload, row, "full_document")

    result.pop("baseline")
    result["estimate"] = "2024 score 48.7 versus 2023 score 48.4"
    assert _source_bundle_from_result(payload, row, "full_document") is not None

    result.update(
        estimate="2022 47.6; 2020 43.6; 2019 42.6",
        period="2019–2022 displayed years",
    )
    assert _source_bundle_from_result(payload, row, "full_document") is not None

    result["estimate"] = "2022 43.6; 2020 47.6; 2019 42.6"
    with pytest.raises(SourceBundleQuantitativeProvenanceError):
        _source_bundle_from_result(payload, row, "full_document")

    result.update(
        estimate="777 cases",
        period="2024",
    )
    narrative_row = {
        **row,
        "text": row["text"] + "\n\nIn 2024, a narrative section reported 777 cases.",
    }
    assert (
        _source_bundle_from_result(
            payload, narrative_row, "full_document"
        )
        is not None
    )

    result["sample"] = "32 respondents"
    narrative_row["text"] += "\nIn 2024, 777 cases occurred among 32 respondents."
    assert (
        _source_bundle_from_result(
            payload, narrative_row, "full_document"
        )
        is not None
    )

    wrong_period_row = {
        **row,
        "text": row["text"] + "\n\nIn 2010, a section reported 32 respondents.",
    }
    result.pop("sample")
    result.update(estimate="32 respondents", period="2024")
    with pytest.raises(SourceBundleQuantitativeProvenanceError):
        _source_bundle_from_result(payload, wrong_period_row, "full_document")

    for period, narrative in (
        ("2024", "In 2024:\n32 respondents were included."),
        (
            "October 29, 2024",
            "On October 29, 2024:\n32 respondents were included.",
        ),
    ):
        result["period"] = period
        wrapped_row = {**row, "text": row["text"] + "\n\n" + narrative}
        assert (
            _source_bundle_from_result(payload, wrapped_row, "full_document")
            is not None
        )

    result.update(estimate="Score 48.7; rank 32", sample="2024", period="2024")
    field_aware_row = {
        **row,
        "text": (
            "In 2024, a sample of 2024 participants had score 48.7 and rank 32.\n\n"
            + row["text"]
        ),
    }
    assert (
        _source_bundle_from_result(payload, field_aware_row, "full_document")
        is not None
    )

    result["sample"] = "2023"
    field_aware_row["text"] = (
        "In 2024, a sample of 2023 participants had score 48.7 and rank 32.\n\n"
        + row["text"]
    )
    assert (
        _source_bundle_from_result(payload, field_aware_row, "full_document")
        is not None
    )


def test_quantitative_provenance_preserves_lower_bound_period_semantics() -> None:
    payload = _bundle_payload()
    result = {
        "estimate": "43,824 deaths",
        "period": "October 7, 2023",
        "provenance": "source_reported",
    }
    payload["evidence_anchors"][0]["quantitative_result"] = result
    row = {
        "source_id": "source-zotero-A1",
        "zotero_item_key": "A1",
        "text": "43,824 deaths were reported since October 7, 2023.",
    }

    with pytest.raises(
        SourceBundleQuantitativeProvenanceError,
        match="period_date_not_local_to_reported_estimate",
    ):
        _source_bundle_from_result(payload, row, "full_document")

    result["period"] = "Since October 7, 2023"
    assert _source_bundle_from_result(payload, row, "full_document") is not None

    row["text"] = (
        "Data from the ministry reported 43,824 deaths on October 7, 2023."
    )
    result["period"] = "October 7, 2023"
    assert _source_bundle_from_result(payload, row, "full_document") is not None


@pytest.mark.parametrize(
    "estimate",
    [
        "-18.5 percentage points",
        "-22.1 percentage points",
        "-42.3 percentage points",
        "-12.7 percentage points",
        "-2.2 percentage points",
        "-56.0 percentage points",
        "-22.7 percentage points",
    ],
)
def test_quantitative_provenance_accepts_wrapped_favorability_deltas(
    estimate,
) -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": estimate,
        "period": "September–December",
        "provenance": "system_derived",
    }
    text = """Net favorability—the percentage of people viewing Israel positively after
subtracting the percentage viewing it negatively—dropped globally by an
average of 18.5 percentage points between September and December,
decreasing in 42 out of the 43 countries polled.
Many countries that already had net negative views of Israel—including Japan,
South Korea, and the U.K.—saw steep declines. Net favorability in Japan went from -39.9 to -62.0; in
South Korea from-5.5 to -47.8; and in the U.K. from -17.1 to -29.8.
The U.S. remains the only rich country that still had net positive views of
Israel. Net favorability dropped just 2.2 percentage points, from a net
favorability of 18.2 to a net favorability of 16 from September to December.
In Egypt, the U.S. went from
having a positive favorability of 41.1 to a negative favorability of -14.9 from
September to December. In Saudi Arabia, the U.S. saw a similar trend, dropping
from a positive favorability of 12.2 to -10.5 over the same time period."""

    assert (
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": text,
            },
            "full_document",
        )
        is not None
    )


def test_quantitative_provenance_accepts_reported_between_month_range() -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "18.5 percentage points",
        "period": "September to December",
        "provenance": "source_reported",
    }
    row = {
        "source_id": "source-zotero-A1",
        "zotero_item_key": "A1",
        "text": (
            "The score dropped by an average of\n"
            "18.5 percentage points between September and December."
        ),
    }

    assert _source_bundle_from_result(payload, row, "full_document") is not None

    row["text"] = (
        "The score dropped by 18.5 percentage points.\n"
        + ("Unrelated prose.\n" * 20)
        + "A separate series ran between September and December."
    )
    with pytest.raises(
        SourceBundleQuantitativeProvenanceError,
        match="period_date_not_local_to_reported_estimate",
    ):
        _source_bundle_from_result(payload, row, "full_document")


def test_quantitative_provenance_accepts_counted_population_modifier() -> None:
    payload = _bundle_payload()
    result = {
        "estimate": "42",
        "denominator": "43 countries polled",
        "population": "43 surveyed countries",
        "period": "September to December",
        "provenance": "source_reported",
    }
    payload["evidence_anchors"][0]["quantitative_result"] = result
    row = {
        "source_id": "source-zotero-A1",
        "zotero_item_key": "A1",
        "text": (
            "Between September and December, the measure decreased in "
            "42 out of the 43 countries polled."
        ),
    }

    assert _source_bundle_from_result(payload, row, "full_document") is not None

    result["population"] = "43 surveyed facilities"
    with pytest.raises(SourceBundleQuantitativeProvenanceError):
        _source_bundle_from_result(payload, row, "full_document")


def test_quantitative_provenance_rejects_unmodeled_anchor_quantity() -> None:
    payload = _bundle_payload()
    anchor = payload["evidence_anchors"][0]
    anchor["claim"] = "The source reports 42 cases among 17 controls."
    anchor["quantitative_result"] = {
        "estimate": "42 cases",
        "provenance": "source_reported",
    }
    row = {
        "source_id": "source-zotero-A1",
        "zotero_item_key": "A1",
        "text": "The source reports 42 cases among 17 controls.",
    }

    with pytest.raises(
        SourceBundleQuantitativeProvenanceError,
        match="quantitative_anchor_contains_unmodeled_quantity",
    ):
        _source_bundle_from_result(payload, row, "full_document")

    anchor["quantitative_result"]["denominator"] = "17 controls"
    assert _source_bundle_from_result(payload, row, "full_document") is not None


def test_quantitative_provenance_isolates_bad_rows_in_rich_bundle() -> None:
    payload = _bundle_payload()
    valid = deepcopy(payload["evidence_anchors"][0])
    valid["evidence_anchor_id"] = ""
    valid.pop("revision_hash", None)
    unmodeled = deepcopy(valid)
    unmodeled.update(
        evidence_anchor_id="anchor-unmodeled",
        claim="The source reports 42 cases among 17 controls.",
        quantitative_result={
            "estimate": "42 cases",
            "provenance": "source_reported",
        },
    )
    remote_period = deepcopy(valid)
    remote_period.update(
        evidence_anchor_id="anchor-remote-period",
        claim="The source reports 43,824 deaths.",
        quantitative_result={
            "estimate": "43,824",
            "period": "October 29, 2024",
            "provenance": "source_reported",
        },
    )
    unmodeled["quantitative_result"]["estimate"] = 42
    payload["evidence_anchors"] = [valid, remote_period]
    payload["component_diagnostics"] = [
        {
            "component": "evidence_anchors",
            "row_index": 0,
            "reason": "ValueError:quantitative result.estimate must be string",
            "raw": unmodeled,
        }
    ]
    row = {
        "source_id": "source-zotero-A1",
        "zotero_item_key": "A1",
        "text": (
            "The source reports 42 cases among 17 controls.\n"
            "Deaths reported: 43,824.\n"
            "Figures as of October 29, 2024 report 43,061 deaths."
        ),
    }

    bundle = _source_bundle_from_result(payload, row, "full_document")

    assert bundle is not None
    assert [anchor.claim for anchor in bundle.evidence_anchors] == [
        valid["claim"]
    ]
    rejected = [
        diagnostic
        for diagnostic in bundle.component_diagnostics
        if diagnostic.get("component") == "evidence_anchors"
        and diagnostic.get("severity") == "rejected"
    ]
    assert {diagnostic["reason"] for diagnostic in rejected} == {
        "SourceBundleQuantitativeProvenanceError:"
        "quantitative_anchor_contains_unmodeled_quantity",
        "SourceBundleQuantitativeProvenanceError:"
        "period_date_not_local_to_reported_estimate",
    }
    assert all(
        diagnostic["rehydrate"] is False
        and isinstance(diagnostic["raw"], dict)
        for diagnostic in rejected
    )
    assert all(
        diagnostic.get("rehydrate") is False
        for diagnostic in bundle.component_diagnostics
        if diagnostic.get("component") == "evidence_anchors"
    )

    replayed = _source_bundle_from_result(bundle.to_dict(), row, "full_document")

    assert replayed is not None
    assert [anchor.claim for anchor in replayed.evidence_anchors] == [
        valid["claim"]
    ]
    assert replayed.component_diagnostics == bundle.component_diagnostics


def test_quantitative_provenance_keeps_all_invalid_bundle_fail_closed() -> None:
    payload = _bundle_payload()
    first = payload["evidence_anchors"][0]
    first["claim"] = "The source reports 42 cases among 17 controls."
    first["quantitative_result"] = {
        "estimate": "42 cases",
        "provenance": "source_reported",
    }
    second = deepcopy(first)
    second.update(
        evidence_anchor_id="anchor-second-invalid",
        claim="The source reports 12 cases among 9 controls.",
        quantitative_result={
            "estimate": "12 cases",
            "provenance": "source_reported",
        },
    )
    payload["evidence_anchors"] = [first, second]

    with pytest.raises(SourceBundleQuantitativeProvenanceError):
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": (
                    "The source reports 42 cases among 17 controls and "
                    "12 cases among 9 controls."
                ),
            },
            "full_document",
        )


@pytest.mark.parametrize(
    ("estimate", "text"),
    [
        (
            "+4.5 score points",
            "The score was 71.8, improving on 67.3 in the previous year.",
        ),
        (
            "-4.5 score points",
            "The score fell from 71.8 to 67.3 in the next year.",
        ),
    ],
)
def test_quantitative_provenance_accepts_explicit_sign_only_deltas(
    estimate, text
) -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": estimate,
        "provenance": "system_derived",
    }

    assert (
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": text,
            },
            "full_document",
        )
        is not None
    )


def test_quantitative_provenance_error_is_typed() -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "43,824",
        "period": "October 29, 2024",
        "provenance": "source_reported",
    }
    with pytest.raises(
        SourceBundleQuantitativeProvenanceError,
        match="period_date_not_found_in_source",
    ):
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
                "text": "Deaths: 43,824.\nUpdated:\n29 Oct 2024.",
            },
            "full_document",
        )


def test_quantitative_provenance_validates_recovered_image_route_text() -> None:
    payload = _bundle_payload()
    payload["evidence_anchors"][0]["quantitative_result"] = {
        "estimate": "999,999",
        "provenance": "source_reported",
    }
    row = {
        "source_id": "source-zotero-A1",
        "zotero_item_key": "A1",
        "text": "The recovered text reports no quantity.",
        "content_route": "pypdf_text_after_codex_image_recovery",
        "document_route": {"recovery": {"state": "completed"}},
    }

    with pytest.raises(SourceBundleQuantitativeProvenanceError):
        _source_bundle_from_result(payload, row, "full_document")

    row["text"] = ""
    with pytest.raises(SourceBundleQuantitativeProvenanceError):
        _source_bundle_from_result(payload, row, "full_document")

    row["content_route"] = "codex_pdf_page_images"
    assert _source_bundle_from_result(payload, row, "full_document") is not None


def test_explicit_retry_replaces_invalid_quantitative_checkpoint(tmp_path) -> None:
    class RetriableBundleReader(BundleReader):
        def read_source_bundle(self, text, metadata, question=None):
            payload = super().read_source_bundle(text, metadata, question)
            if self.calls == 1:
                payload["evidence_anchors"][0]["quantitative_result"] = {
                    "estimate": "43,824",
                    "period": "October 29, 2024",
                    "provenance": "source_reported",
                }
            return payload

    item = {
        "key": "ITEMA",
        "data": {
            "key": "ITEMA",
            "itemType": "report",
            "title": "Retriable report",
            "date": "2024",
        },
    }
    reader = RetriableBundleReader()
    request = MapRequest(
        tmp_path, provider="ollama", model="bundle-v1", parallel=1
    )

    first = run_map(
        request,
        client=FakeZotero([item]),
        reader=reader,
        run_id="retry-invalid-quantitative",
    )
    retried = resume_map(
        tmp_path,
        "retry-invalid-quantitative",
        retry_terminal_failures=True,
        client=FakeZotero([item]),
        reader=reader,
    )

    assert first.parked_for_review_count == 1
    assert retried.validated_note_count == 1
    assert reader.calls == 2
    assert list((tmp_path / "02_source_memory" / "notes").glob("*.md"))
    assert not (
        tmp_path
        / "11_state"
        / "runs"
        / "retry-invalid-quantitative"
        / "items"
        / "ITEMA"
        / "source_recovery.yml"
    ).exists()


def test_reader_failure_checkpoint_preserves_raw_response_and_completion(
    tmp_path,
) -> None:
    class RawFailureReader(BundleReader):
        def read_source_bundle(self, *_args, **_kwargs):
            exc = ProviderError("invalid source bundle")
            exc.raw_response = '{"truncated":'
            exc.provider_completion = {"finish_reason": "length"}
            raise exc

    item = {
        "key": "ITEMA",
        "data": {
            "key": "ITEMA",
            "itemType": "report",
            "title": "A report",
        },
    }
    report = run_map(
        MapRequest(tmp_path, provider="ollama", model="bundle-v1", parallel=1),
        client=FakeZotero([item]),
        reader=RawFailureReader(),
        run_id="raw-failure",
    )

    assert report.parked_for_review_count == 1
    failure = read_yaml(
        tmp_path
        / "11_state"
        / "runs"
        / "raw-failure"
        / "items"
        / "ITEMA"
        / "source_failure.yml"
    )
    assert failure["raw_response"] == '{"truncated":'
    assert failure["provider_completion"]["finish_reason"] == "length"


@pytest.mark.parametrize("component", ["identity", "literature"])
def test_bundle_rejects_other_source_ownership(component: str) -> None:
    payload = deepcopy(_bundle_payload())
    if component == "identity":
        payload["source_identity"]["source_id"] = "source-zotero-other"
    else:
        payload["literature_positions"][0][
            "current_source_id"
        ] = "source-zotero-other"

    with pytest.raises(ValueError, match="does not match requested source"):
        _source_bundle_from_result(
            payload,
            {
                "source_id": "source-zotero-A1",
                "zotero_item_key": "A1",
            },
            "partial_document",
        )


def test_extraction_scope_overrides_model_scope_without_losing_diagnostic() -> None:
    payload = deepcopy(_bundle_payload())
    payload["scope_assessment"] = {
        "source_scope": "full_document",
        "evidence_eligibility": "substantive_full",
    }

    bundle = _source_bundle_from_result(
        payload,
        {"source_id": "source-zotero-A1", "zotero_item_key": "A1"},
        "partial_document",
    )

    assert bundle is not None
    assert bundle.scope_assessment["source_scope"] == "partial_document"
    assert bundle.scope_assessment["evidence_eligibility"] == "substantive_bounded"
    assert bundle.scope_assessment["model_source_scope"] == "full_document"
    assert (
        bundle.scope_assessment["model_evidence_eligibility"]
        == "substantive_full"
    )


def test_pipeline_normalizes_descriptive_support_envelopes_and_missing_sources() -> None:
    payload = deepcopy(_bundle_payload())
    payload["evidence_anchors"][0]["planning_roles"] = "major_finding"
    payload["evidence_anchors"][0]["support_envelope"] = {
        "empirical_role": "Statistical result",
        "argument_role": "Key evidence for thesis",
        "coverage": "Cox regression with 175 subjects",
        "scope": "Civil wars 1946-2005",
        "restrictions": "Observational design",
        "support_status": "Robust to model specifications",
    }
    payload["missing_source_recommendations"] = [
        {
            "current_source_id": "source-zotero-A1",
            "raw_citation": "Walter 1997",
            "author": "Walter",
            "year": "1997",
            "title": "The Critical Barrier",
            "identifiers": {},
            "engagement": "Theoretical foundation",
            "relation_label": "builds_on",
            "locator": "p. 4",
        }
    ]
    payload["component_diagnostics"] = [
        {
            "component": "evidence_anchors",
            "row_index": 0,
            "reason": "provider helper shape",
            "raw": payload["evidence_anchors"][0],
        },
        {
            "component": "literature_positions",
            "row_index": 0,
            "reason": "provider helper shape",
            "raw": {
                **payload["literature_positions"][0],
                "flat_author": "Walter, Barbara",
                "author": "",
                "year": 1997,
                "identifiers": "DOI 10.0000/example",
            },
        },
        {
            "component": "missing_source_recommendations",
            "row_index": 0,
            "reason": "provider helper shape",
            "raw": payload["missing_source_recommendations"][0],
        },
    ]
    payload["component_diagnostics"].extend(
        deepcopy(payload["component_diagnostics"][:2])
    )

    bundle = _source_bundle_from_result(
        payload,
        {"source_id": "source-zotero-A1", "zotero_item_key": "A1"},
        "full_document",
    )

    assert bundle is not None
    assert len(bundle.evidence_anchors) == 1
    assert len(bundle.literature_positions) == 1
    envelope = bundle.evidence_anchors[0].support_envelope
    assert envelope.empirical_role == "associational"
    assert bundle.evidence_anchors[0].planning_roles == ["major_finding"]
    assert envelope.scope == {"description": ["Civil wars 1946-2005"]}
    assert envelope.restrictions == ["Observational design"]
    assert envelope.support_status == "supported"
    recommendation = bundle.missing_source_recommendations[0]
    assert bundle.literature_positions[0].author == "Walter"
    assert bundle.literature_positions[0].year == "1997"
    assert bundle.literature_positions[0].identifiers == {}
    assert recommendation.normalized_citation["title"] == "The Critical Barrier"
    assert recommendation.discussed_by_source_ids == ["source-zotero-A1"]


def test_new_source_resolves_prior_literature_position_without_rereading_old_source(
    tmp_path,
) -> None:
    citing = {
        "key": "ITEMA",
        "data": {
            "key": "ITEMA",
            "itemType": "journalArticle",
            "title": "Institutions and Reform",
            "date": "2024",
            "creators": [{"creatorType": "author", "lastName": "One"}],
        },
    }
    cited = {
        "key": "ITEMB",
        "data": {
            "key": "ITEMB",
            "itemType": "journalArticle",
            "title": "The Critical Barrier to Civil War Settlement",
            "date": "1997",
            "creators": [{"creatorType": "author", "lastName": "Walter"}],
        },
    }
    reader = BundleReader()
    first = run_map(
        MapRequest(tmp_path, provider="ollama", model="bundle-v1", parallel=1),
        client=FakeZotero([citing]),
        reader=reader,
        run_id="literature-match-one",
    )

    run_map(
        MapRequest(tmp_path, provider="ollama", model="bundle-v1", parallel=1),
        client=FakeZotero([citing, cited]),
        reader=reader,
        run_id="literature-match-two",
    )

    assert reader.calls == 2
    citing_note = tmp_path / first.items[0]["note_path"]
    text = citing_note.read_text(encoding="utf-8")
    assert "[[" in text
    assert "Walter (1997)" in text
    positions = read_yaml(
        tmp_path / "02_source_memory" / "indexes" / "literature_positions.yml"
    )["positions"]
    citing_position = next(
        row
        for row in positions
        if row["current_source_id"] == "source-zotero-itema"
    )
    assert citing_position["matched_source_id"] == "source-zotero-itemb"


def test_zotero_metadata_correction_updates_projection_without_source_call(
    tmp_path,
) -> None:
    original = {
        "key": "ITEMA",
        "data": {
            "key": "ITEMA",
            "itemType": "journalArticle",
            "title": "Uncorrected title",
            "date": "2024",
            "creators": [{"creatorType": "author", "lastName": "One"}],
        },
    }
    corrected = {
        **original,
        "data": {
            **original["data"],
            "title": "Corrected canonical title",
        },
    }
    reader = BundleReader()
    first = run_map(
        MapRequest(tmp_path, provider="ollama", model="bundle-v1", parallel=1),
        client=FakeZotero([original]),
        reader=reader,
        run_id="metadata-one",
    )
    second = run_map(
        MapRequest(tmp_path, provider="ollama", model="bundle-v1", parallel=1),
        client=FakeZotero([corrected]),
        reader=reader,
        run_id="metadata-two",
    )

    assert reader.calls == 1
    assert second.reused_count == 1
    note = tmp_path / first.items[0]["note_path"]
    assert "# Corrected canonical title" in note.read_text(encoding="utf-8")


def test_zotero_document_type_change_invalidates_source_bundle(tmp_path) -> None:
    original = {
        "key": "ITEMA",
        "data": {
            "key": "ITEMA",
            "itemType": "book",
            "title": "Institutional study",
            "date": "2024",
            "creators": [{"creatorType": "author", "lastName": "One"}],
        },
    }
    corrected = {
        **original,
        "data": {
            **original["data"],
            "itemType": "report",
        },
    }
    reader = BundleReader()
    run_map(
        MapRequest(tmp_path, provider="ollama", model="bundle-v1", parallel=1),
        client=FakeZotero([original]),
        reader=reader,
        run_id="type-one",
    )
    second = run_map(
        MapRequest(tmp_path, provider="ollama", model="bundle-v1", parallel=1),
        client=FakeZotero([corrected]),
        reader=reader,
        run_id="type-two",
    )

    assert reader.calls == 2
    assert second.reused_count == 0


def test_reprocessing_source_replaces_its_stale_literature_memory(tmp_path) -> None:
    item = {
        "key": "ITEMA",
        "data": {
            "key": "ITEMA",
            "itemType": "journalArticle",
            "title": "Institutions and Reform",
            "date": "2024",
            "creators": [{"creatorType": "author", "lastName": "One"}],
        },
    }
    first = run_map(
        MapRequest(tmp_path, provider="ollama", model="bundle-v1", parallel=1),
        client=FakeZotero([item]),
        reader=BundleReader(),
        run_id="literature-stale-one",
    )
    payload = _bundle_payload()
    payload["source_identity"] = {
        "source_id": "source-zotero-itema",
        "zotero_key": "ITEMA",
    }
    payload["evidence_anchors"][0]["source_id"] = "source-zotero-itema"
    payload["literature_positions"] = []
    payload["missing_source_recommendations"] = []

    _commit_literature_memory(
        tmp_path,
        SourceAnalysisBundle.from_dict(payload),
        tmp_path / first.items[0]["note_path"],
    )

    positions = read_yaml(
        tmp_path / "02_source_memory" / "indexes" / "literature_positions.yml"
    )["positions"]
    missing = read_yaml(
        tmp_path / "02_source_memory" / "indexes" / "missing_sources.yml"
    )["sources"]
    assert not any(
        row["current_source_id"] == "source-zotero-itema" for row in positions
    )
    assert not any(
        "source-zotero-itema" in row.get("discussed_by_source_ids", [])
        for row in missing
    )


def test_hierarchical_source_synthesis_returns_the_canonical_bundle(tmp_path) -> None:
    class HierarchicalReader:
        name = "hierarchical"
        model = "bundle-v1"
        is_cloud = False
        chunk_output_tokens = []

        def summarize_chunk(self, text, metadata, question=None, **kwargs):
            del text, metadata, question
            self.chunk_output_tokens.append(kwargs["max_output_tokens"])
            return {"claim": "Bounded chunk evidence."}

        def synthesize_document_bundle(
            self, chunk_memos, metadata, question=None, **kwargs
        ):
            del chunk_memos, question, kwargs
            payload = _bundle_payload()
            context = metadata["_source_context"]
            payload["source_identity"] = {
                "source_id": context["source_id"],
                "zotero_key": context["zotero_key"],
            }
            payload["evidence_anchors"][0]["source_id"] = context["source_id"]
            payload["literature_positions"][0]["current_source_id"] = context[
                "source_id"
            ]
            return payload

    request = MapRequest(
        tmp_path,
        provider="ollama",
        model="bundle-v1",
        processing=ProcessingPolicy(
            direct_read_char_limit=50,
            chunk_char_limit=100,
            max_total_chunks=20,
            max_calls_per_document_run=20,
        ),
    )
    result, route, _reason = _read_document(
        HierarchicalReader(),
        "Substantive source evidence. " * 40,
        {
            "_source_context": {
                "source_id": "source-zotero-A1",
                "zotero_key": "A1",
            }
        },
        None,
        request=request,
        checkpoint_root=tmp_path / "checkpoint",
    )

    assert result["bundle_schema_version"] == "1"
    assert route == "hierarchical_hierarchical_text"
    assert set(HierarchicalReader.chunk_output_tokens) == {8_000}
