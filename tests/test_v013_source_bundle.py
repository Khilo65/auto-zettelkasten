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
from auto_zettelkasten.api import run_map
from auto_zettelkasten.files import read_yaml
from auto_zettelkasten.notes import read_note
from auto_zettelkasten.pipeline import (
    _ProfileProviderBudget,
    _commit_literature_memory,
    _commit_remediation_ledgers,
    _match_literature_position,
    _read_document,
    _source_bundle_from_result,
)
from auto_zettelkasten.readers import DeepSeekReader

from conftest import FakeZotero


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
    assert note["frontmatter"]["source_bundle_prompt_version"] == "4"


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
    with pytest.raises(RuntimeError, match="source_profile_call_budget_reached"):
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

    assert reader.calls == 1
    assert resumed.max_calls == 1
    assert resumed.cumulative_calls == 1
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
    payload["evidence_anchors"] = []
    payload["literature_positions"] = []
    payload["missing_source_recommendations"] = []

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
    assert bundle.literature_positions[0].author == "Walter, Barbara"
    assert bundle.literature_positions[0].year == "1997"
    assert bundle.literature_positions[0].identifiers == {
        "other": "DOI 10.0000/example"
    }
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

        def summarize_chunk(self, text, metadata, question=None, **kwargs):
            del text, metadata, question, kwargs
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
