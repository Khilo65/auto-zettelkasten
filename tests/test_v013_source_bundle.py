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
    _ProfileProviderBudget,
    _commit_literature_memory,
    _commit_remediation_ledgers,
    _literature_position_relations,
    _match_literature_position,
    _match_literature_position_detail,
    _read_document,
    _recover_saved_source_bundle,
    _source_bundle_from_result,
)
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
    assert profile["context"]["profile_generation_route"] == "source_analysis_bundle"
    assert profile["context"]["source_analysis_bundle_dependency_fingerprint"]
    assert profile["context"]["thesis"] == "Monitoring changes implementation."
    assert profile["methods"][0] == "Comparative qualitative analysis."
    assert profile["concepts"] == ["credible commitment"]
    assert profile["theories"] == ["commitment problem"]
    assert profile["mechanisms"] == ["monitoring"]
    assert profile["outcomes"] == ["implementation"]
    assert profile["source_role"] == "journal article"
    assert profile["coverage"]["status"] == "partial"
    note = read_note(tmp_path / report.items[0]["note_path"])
    assert note["frontmatter"]["source_bundle_prompt_version"] == "5"


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


def test_old_bundle_diagnostics_migrate_locally_without_rewriting_note(
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
    request = MapRequest(
        tmp_path, provider="ollama", model="bundle-v1", parallel=1
    )
    first = run_map(
        request,
        client=FakeZotero([item]),
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
        client=FakeZotero([item]),
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
