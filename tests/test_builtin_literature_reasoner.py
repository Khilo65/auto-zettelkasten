from __future__ import annotations

import threading
import time
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest
import yaml

from auto_zettelkasten import readers as reader_module
from auto_zettelkasten.api import build_map, get_status, resume_map, run_map
from auto_zettelkasten.models import (
    EvidenceProfile,
    LiteratureMapRequest,
    LiteratureMappingPolicy,
    MapRequest,
)
from auto_zettelkasten.notes import read_note
from auto_zettelkasten.ports import LiteratureReasoner
from auto_zettelkasten.profiles import (
    deterministic_profile,
    load_profile_sidecar,
    profile_dependency_fingerprint,
    profile_to_dict,
    save_profile,
)
from auto_zettelkasten.readers import (
    CloudPermissionError,
    DeepSeekReader,
    GeminiReader,
    OllamaReader,
    OpenRouterReader,
    ProviderError,
    _parse_json_object,
    _validate_literature_response,
    _validate_relationship_response,
)

from conftest import SECTION_KEYS, FakeReader, FakeZotero


def _analysis() -> dict[str, str]:
    return {key: f"Source-grounded {key}; see page 1." for key in SECTION_KEYS}


def _bundle_from_prompt(prompt: str, marker: str = "bundle-profile") -> dict[str, Any]:
    source_match = re.search(r'"source_id":\s*"([^"]+)"', prompt)
    key_match = re.search(r'"zotero_key":\s*"([^"]+)"', prompt)
    assert source_match and key_match
    source_id = source_match.group(1)
    return {
        "bundle_schema_version": "1",
        "source_identity": {
            "source_id": source_id,
            "zotero_key": key_match.group(1),
        },
        "observed_bibliographic_identity": {},
        "scope_assessment": {
            "source_scope": "full_document",
            "evidence_eligibility": "substantive_bounded",
        },
        "analysis_sections": _analysis(),
        "compact_profile": {
            "thesis": "The source advances a bounded institutional argument.",
            "method_or_knowledge_basis": "Source-grounded analysis.",
            "source_genre": "journal article",
            "inferential_design": "descriptive",
            "concepts": [marker],
        },
        "evidence_anchors": [
            {
                "evidence_anchor_id": f"anchor-{source_id}",
                "source_id": source_id,
                "claim": "The source advances a bounded institutional argument.",
                "locator": "p. 1",
                "planning_roles": ["thesis", "major_finding"],
                "salience_priority": 10,
            }
        ],
        "literature_positions": [],
        "missing_source_recommendations": [],
        "self_review": {"passed": True},
    }


def _profile_response(note_text: str, marker: str) -> dict[str, Any]:
    payload = profile_to_dict(deterministic_profile(note_text))
    payload["concepts"] = [marker]
    payload["theories"] = [f"{marker} theory"]
    payload["mechanisms"] = [f"{marker} mechanism"]
    return payload


def _profile_from_prompt(prompt: str, marker: str) -> dict[str, Any]:
    delimiter = "COMMITTED MARKDOWN NOTE:\n"
    assert delimiter in prompt
    return _profile_response(prompt.split(delimiter, 1)[1], marker)


def _progress(workspace: Path, run_id: str) -> dict[str, Any]:
    return yaml.safe_load(
        (workspace / "11_state" / "runs" / run_id / "progress.yml").read_text(
            encoding="utf-8"
        )
    )


def _only_profile(workspace: Path) -> EvidenceProfile:
    sidecars = list((workspace / "02_source_memory" / "profiles").glob("*.yml"))
    assert len(sidecars) == 1
    return load_profile_sidecar(sidecars[0])


def test_literature_json_parser_recovers_one_object_wrapped_in_provider_prose() -> None:
    assert _parse_json_object(
        'Here is the requested result:\n{"gaps": [], "rejected": []}\nDone.',
        label="gap adjudication response",
    ) == {"gaps": [], "rejected": []}
    assert _parse_json_object(
        'Here is the requested result:\n{"analysis": {"thesis": "Nested"}}\nDone.',
        label="source bundle response",
    ) == {"analysis": {"thesis": "Nested"}}

    with pytest.raises(ProviderError, match="was not valid JSON"):
        _parse_json_object(
            '{"gaps": []}\n{"rejected": []}',
            label="gap adjudication response",
        )


def test_relationship_response_accepts_bare_lists_and_preserves_malformed_rows() -> None:
    assert _parse_json_object(
        '[{"source_id": "a"}, "malformed"]',
        label="relationship candidate response",
        list_key="candidates",
    ) == {"candidates": [{"source_id": "a"}, "malformed"]}
    assert _validate_relationship_response(
        {"decisions": [{"source_id": "a"}, "malformed"]},
        kind="relationship_adjudication",
    ) == {"decisions": [{"source_id": "a"}, "malformed"]}


def test_builtin_gap_response_normalizes_optional_model_shape_errors_per_candidate() -> (
    None
):
    normalized = _validate_literature_response(
        {
            "gaps": [
                {
                    "gap_id": "gap-1",
                    "proposition_id": "proposition-1",
                    "originating_proposition_id": "proposition-1",
                    "originating_cluster_ids": ["cluster-1"],
                    "priority_tier": "medium",
                    "countervailing_evidence": ["not an evidence object"],
                    "value_assessment": {
                        "information_gain": "medium",
                        "competing_explanations": "selection into mediation",
                        "non_obviousness_passed": "true",
                        "importance_passed": True,
                    },
                    "study_design": {
                        "outcomes": "one outcome",
                        "validity_risks": "spillover across comparison cases",
                        "identification_or_inference_stategy": "matched comparison",
                        "ethical_constraints": ["standard research ethics"],
                    },
                    "resolution_path": {
                        "type": "qualitative",
                        "research_question": "Which process distinguishes the competing explanations?",
                        "needed_evidence": "Within-case observations of the proposed mechanism.",
                        "requirements": {
                            "case_selection": "Select most-likely and least-likely cases.",
                            "mechanism_evidence": "Trace the proposed intervening process.",
                            "negative_cases": "Include cases where the expected process is absent.",
                            "process_observations": "Use temporally ordered diagnostic observations.",
                        },
                        "feasibility": "Feasible with the archives named in the collection.",
                        "constraints": "Archive access may be incomplete.",
                    },
                    "anchors": [
                        {"cluster_id": "", "section": "central_findings", "item_id": ""}
                    ],
                }
            ],
            "rejected": [],
        },
        kind="gap_adjudication",
    )

    gap = normalized["gaps"][0]
    assert gap["priority_tier"] == "moderate"
    assert gap["countervailing_evidence"] == []
    assert gap["value_assessment"]["information_gain"] == "moderate"
    assert gap["value_assessment"]["competing_explanations"] == [
        "selection into mediation"
    ]
    assert gap["value_assessment"]["non_obviousness_passed"] is False
    assert gap["study_design"]["outcomes"] == ["one outcome"]
    assert gap["study_design"]["validity_risks"] == [
        "spillover across comparison cases"
    ]
    assert (
        gap["study_design"]["identification_or_inference_strategy"]
        == "matched comparison"
    )
    assert gap["study_design"]["ethical_constraints"] == "standard research ethics"
    assert gap["proposition_id"] == "proposition-1"
    assert gap["originating_cluster_ids"] == ["cluster-1"]
    assert gap["resolution_path"]["path_type"] == "qualitative"
    assert gap["resolution_path"]["limitations"] == [
        "Archive access may be incomplete."
    ]
    assert gap["anchors"] == []


def test_builtin_cluster_response_drops_only_malformed_optional_assertions() -> None:
    normalized = _validate_literature_response(
        {
            "cluster_id": "cluster-1",
            "effective_evidence_base_count": 999,
            "evidence_base_groups": [
                {
                    "evidence_base_group_id": "model-group",
                    "counted_as_independent": True,
                }
            ],
            "independence_assessments": [
                {"evidence_base_group_id": "model-group", "independent": True}
            ],
            "quantitative_comparisons": [
                {"metric": "model-authored rate", "source_estimates": ["12%", "15%"]}
            ],
            "central_findings": [
                {
                    "finding": "Mediator strategy is associated with success.",
                    "evidence": [
                        {
                            "source_id": "source-a",
                            "evidence_anchor_id": "anchor-a",
                            "locator": "p. 15",
                        }
                    ],
                }
            ],
            "synthesis_assertions": [
                {
                    "assertion": "Mediator strategy is associated with success.",
                    "evidence": ["Bercovitch1991 p.15"],
                }
            ],
        },
        kind="cluster_synthesis",
    )

    assert normalized["synthesis_assertions"] == []
    assert normalized["central_findings"][0]["evidence"][0]["source_id"] == "source-a"
    assert normalized["effective_evidence_base_count"] == 0
    assert normalized["evidence_base_groups"] == []
    assert normalized["independence_assessments"] == []
    assert normalized["quantitative_comparisons"] == []


def test_builtin_cluster_proposal_ignores_model_authored_independence_fields() -> None:
    normalized = _validate_literature_response(
        {
            "clusters": [
                {
                    "cluster_id": "model-authored-cluster-id",
                    "proposal_id": "proposal-1",
                    "label": "Comparable mediation findings",
                    "semantic_identity": "mediation-findings",
                    "shared_question": "Which conditions shape mediation outcomes?",
                    "coherence_rationale": "The sources address the same bounded proposition.",
                    "source_ids": ["source-a", "source-b"],
                    "supporting_evidence": [],
                    "propositions": [],
                    "source_roles": {"source-a": "core", "source-b": "core"},
                    "study_lineages": ["lineage-model-authored"],
                    "evidence_base_groups": ["evidence-base-model-authored"],
                    "independence_assessments": [
                        {
                            "source_id": "source-a",
                            "independence_status": "assumed_independent",
                        }
                    ],
                    "effective_evidence_base_count": 999,
                }
            ]
        },
        kind="cluster_proposal",
    )

    proposal = normalized["clusters"][0]
    assert proposal.get("cluster_id", "") == ""
    assert proposal["study_lineages"] == []
    assert proposal["evidence_base_groups"] == []
    assert proposal["independence_assessments"] == []
    assert proposal["effective_evidence_base_count"] == 0


def test_builtin_cluster_proposal_preserves_scalar_comparability_as_provider_assessment() -> (
    None
):
    normalized = _validate_literature_response(
        {
            "clusters": [
                {
                    "proposal_id": "proposal-1",
                    "label": "Conflict prevention",
                    "semantic_identity": "conflict-prevention",
                    "shared_question": "How does mediation contribute to conflict prevention?",
                    "coherence_rationale": "The sources address a recognizable bounded literature.",
                    "source_ids": ["source-a", "source-b"],
                    "supporting_evidence": [],
                    "propositions": [
                        {
                            "proposition_id": "proposition-1",
                            "statement": "Preventive diplomacy can interrupt escalation.",
                            "proposition_type": "empirical",
                            "source_ids": ["source-a", "source-b"],
                            "evidence": [],
                            "comparability": "The studies examine different settings but the same preventive function.",
                        }
                    ],
                    "family_relations": [
                        {
                            "relation_type": "shared_research_problem",
                            "source_ids": ["source-a", "source-b"],
                            "rationale": "Both sources ask how mediation can prevent escalation.",
                            "evidence": [],
                            "comparability": "Comparable at the level of the research problem, not a common estimand.",
                        }
                    ],
                    "source_roles": {"source-a": "core", "source-b": "core"},
                }
            ]
        },
        kind="cluster_proposal",
    )

    proposal = normalized["clusters"][0]
    assert proposal["propositions"][0]["comparability"] == {
        "provider_assessment": "The studies examine different settings but the same preventive function."
    }
    assert proposal["family_relations"][0]["comparability"] == {
        "provider_assessment": "Comparable at the level of the research problem, not a common estimand."
    }


def test_builtin_cluster_synthesis_normalizes_provider_contribution_labels() -> None:
    normalized = _validate_literature_response(
        {
            "cluster_id": "cluster-1",
            "synthesis": "The sources address one bounded research problem.",
            "debate_explanation": (
                "They examine different propositions, so no strict debate is established."
            ),
            "source_contributions": [
                {
                    "contribution_id": "contribution-1",
                    "source_id": "source-a",
                    "cluster_role": "core",
                    "contribution_kind": "empirical finding",
                    "related_proposition_ids": [],
                    "evidence_thread_id": "thread-1",
                    "finding": "The source reports one relevant finding.",
                    "technical_result": "",
                    "plain_english_meaning": "It adds source-specific evidence.",
                    "relation_to_cluster_question": "It addresses the cluster question.",
                    "comparison_status": "not compared",
                    "evidence": [
                        {
                            "source_id": "source-a",
                            "claim_id": "anchor-a",
                            "locator": "p. 12",
                        }
                    ],
                }
            ],
        },
        kind="cluster_synthesis",
    )

    contribution = normalized["source_contributions"][0]
    assert "no strict debate is established" in normalized["synthesis"]
    assert contribution["contribution_kind"] == "unique_cluster_relevant_finding"
    assert contribution["comparison_status"] == "single_source"


def test_builtin_cluster_synthesis_normalizes_optional_contribution_shapes() -> None:
    normalized = _validate_literature_response(
        {
            "cluster_id": "cluster-1",
            "source_contributions": [
                {
                    "source_id": "source-a",
                    "cluster_role": "context",
                    "contribution_kind": "context_only",
                    "related_proposition_ids": None,
                    "evidence_thread_id": None,
                    "finding": "The source provides conceptual context.",
                    "technical_result": None,
                    "plain_english_meaning": "It explains the framework.",
                    "relation_to_cluster_question": None,
                    "comparison_status": "single_source",
                    "origin": None,
                    "evidence": {
                        "source_id": "source-a",
                        "claim_id": "anchor-a",
                        "locator": "p. 12",
                    },
                }
            ],
        },
        kind="cluster_synthesis",
    )

    contribution = normalized["source_contributions"][0]
    assert contribution["contribution_kind"] == "conceptual_context"
    assert contribution["related_proposition_ids"] == []
    assert contribution["evidence_thread_id"] == ""
    assert contribution["technical_result"] == ""
    assert contribution["relation_to_cluster_question"] == ""
    assert contribution["origin"] == "reasoner"
    assert contribution["evidence"] == [
        {
            "source_id": "source-a",
            "claim_id": "anchor-a",
            "locator": "p. 12",
        }
    ]


def test_builtin_cluster_synthesis_drops_bare_anchor_ids_without_losing_response() -> None:
    normalized = _validate_literature_response(
        {
            "cluster_id": "cluster-1",
            "evidence_threads": [
                {
                    "thread_id": "thread-1",
                    "title": "Implementation guidance",
                    "question": "What do the documents recommend?",
                    "summary": "The documents offer implementation guidance.",
                    "plain_english_meaning": "They explain what practitioners should do.",
                    "relationship": "Complementary guidance.",
                    "source_ids": ["source-a"],
                    "proposition_ids": [],
                    "evidence": ["anchor-a"],
                }
            ],
        },
        kind="cluster_synthesis",
    )

    assert normalized["evidence_threads"][0]["evidence"] == []


def test_builtin_cluster_synthesis_accepts_known_evidentiary_threads_alias() -> None:
    normalized = _validate_literature_response(
        {
            "cluster_id": "cluster-1",
            "evidentiary_threads": [
                {
                    "thread_id": "thread-1",
                    "title": "Institutional practice",
                    "summary": "The sources document complementary institutional practices.",
                    "relationship": "complementary",
                    "evidence": [
                        {
                            "source_id": "source-a",
                            "claim_id": "anchor-a",
                            "locator": "p. 12",
                        }
                    ],
                }
            ],
        },
        kind="cluster_synthesis",
    )

    assert len(normalized["evidence_threads"]) == 1
    assert normalized["evidence_threads"][0]["thread_id"] == "thread-1"


def test_builtin_cluster_synthesis_discards_noncanonical_repair_explanation() -> None:
    normalized = _validate_literature_response(
        {
            "cluster_id": "cluster-1",
            "explanation": "I corrected the fields requested by the validator.",
        },
        kind="cluster_synthesis",
    )

    assert normalized["cluster_id"] == "cluster-1"
    assert "explanation" not in normalized

    with pytest.raises(ProviderError, match="unknown cluster synthesis fields"):
        _validate_literature_response(
            {"cluster_id": "cluster-1", "invented_substantive_field": "value"},
            kind="cluster_synthesis",
        )


class _SourceOnlyDeepSeek:
    name = "deepseek"
    model = "deepseek-v4-flash"
    is_cloud = True

    def __init__(self) -> None:
        self.calls = 0

    def read_source(
        self, text: str, metadata: Mapping[str, Any], question: str | None = None
    ) -> Mapping[str, Any]:
        self.calls += 1
        return _analysis()


class _ExplicitReasoner:
    name = "explicit-reasoner"
    model = "explicit-v1"
    is_cloud = False

    def __init__(self) -> None:
        self.profile_calls = 0
        self.request_deadline = 0.0

    def profile_source(
        self,
        note: Mapping[str, Any],
        *,
        question: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        self.profile_calls += 1
        assert context and context["profile_prompt_version"] == "6"
        return _profile_response(str(note["committed_note"]), "explicit-profile")

    def propose_clusters(
        self,
        profiles: Sequence[EvidenceProfile],
        request: LiteratureMapRequest,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        return {"clusters": []}

    def map_debates(
        self,
        profiles: Sequence[EvidenceProfile],
        request: LiteratureMapRequest,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        return {"assessments": []}

    def detect_gaps(
        self,
        profiles: Sequence[EvidenceProfile],
        request: LiteratureMapRequest,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        return {"gaps": []}


class _ConcurrentReasoner(_ExplicitReasoner):
    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self.active_calls = 0
        self.max_active_calls = 0

    def profile_source(self, note, *, question=None, context=None):
        with self._lock:
            self.profile_calls += 1
            self.active_calls += 1
            self.max_active_calls = max(self.max_active_calls, self.active_calls)
        try:
            time.sleep(0.05)
            return _profile_response(str(note["committed_note"]), "parallel-profile")
        finally:
            with self._lock:
                self.active_calls -= 1


class _RelationshipThenClusterFailureReasoner(_ExplicitReasoner):
    def select_relationship_candidates(
        self,
        profiles: Sequence[EvidenceProfile],
        request: LiteratureMapRequest,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        left, right = profiles[:2]
        return {
            "candidates": [
                {
                    "source_id": left.source_id,
                    "target_kind": "source",
                    "target_id": right.source_id,
                    "why_relevant": "Both sources address the same bounded institutional proposition.",
                    "comparison_unit": "institutional proposition",
                    "confidence": 0.9,
                }
            ]
        }

    def adjudicate_relationships(
        self,
        profiles: Sequence[EvidenceProfile],
        request: LiteratureMapRequest,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        assert context
        return {
            "decisions": [
                {
                    "pair_job_id": job["pair_job_id"],
                    "decision": "relationship",
                    "pair": job["pair"],
                    "relation_type": "complements",
                    "actor_source_id": job["pair"]["left_source_id"],
                    "reference_source_id": job["pair"]["right_source_id"],
                    "forward_label": "complements",
                    "inverse_label": "complements",
                    "comparison_proposition": "The sources address the same bounded institutional proposition.",
                    "reason": "The sources provide complementary evidence for the same bounded institutional proposition.",
                    "left_evidence_anchor_ids": [
                        job["selected_evidence"]["left"][0]["evidence_anchor_id"]
                    ],
                    "right_evidence_anchor_ids": [
                        job["selected_evidence"]["right"][0]["evidence_anchor_id"]
                    ],
                    "confidence": "high",
                    "output_contract": "relationship-decision-v4",
                }
                for job in context["pair_jobs"]
            ]
        }

    def propose_clusters(
        self,
        profiles: Sequence[EvidenceProfile],
        request: LiteratureMapRequest,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        assert context and any(
            row.get("relation_type") == "complements"
            for row in context["accepted_relationships"]
        )
        raise RuntimeError("cluster provider unavailable")


class _ReplayableRelationshipReasoner(_RelationshipThenClusterFailureReasoner):
    def __init__(self) -> None:
        super().__init__()
        self.candidate_calls = 0
        self.adjudication_calls = 0
        self.cluster_calls = 0

    def select_relationship_candidates(self, *args: Any, **kwargs: Any) -> Mapping[str, Any]:
        self.candidate_calls += 1
        return super().select_relationship_candidates(*args, **kwargs)

    def adjudicate_relationships(self, *args: Any, **kwargs: Any) -> Mapping[str, Any]:
        self.adjudication_calls += 1
        return super().adjudicate_relationships(*args, **kwargs)

    def propose_clusters(
        self,
        profiles: Sequence[EvidenceProfile],
        request: LiteratureMapRequest,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        self.cluster_calls += 1
        return {"clusters": []}


class _RecoveringReasoner(_ExplicitReasoner):
    def __init__(self, fail_keys: set[str] | None = None) -> None:
        super().__init__()
        self.fail_keys = fail_keys or set()

    def profile_source(self, note, *, question=None, context=None):
        self.profile_calls += 1
        if str(note.get("zotero_item_key") or "") in self.fail_keys:
            raise RuntimeError("malformed_profile_response")
        return _profile_response(str(note["committed_note"]), "recovered-profile")


class _ContractInvalidReasoner(_ExplicitReasoner):
    def profile_source(self, note, *, question=None, context=None):
        self.profile_calls += 1
        response = _profile_response(
            str(note["committed_note"]), "contract-invalid-profile"
        )
        response["unexpected_profile_field"] = "not allowed"
        return response


class _RecoveringSourceReader(FakeReader):
    def __init__(self, fail_keys: set[str] | None = None) -> None:
        super().__init__()
        self.fail_keys = fail_keys or set()

    def read_source(self, text, metadata, question=None):
        self.calls += 1
        if str(metadata.get("key") or "") in self.fail_keys:
            raise RuntimeError("transient_source_failure")
        return _analysis()


def test_configured_builtin_reader_profiles_by_default_and_totals_live_calls(
    tmp_path: Path,
    sample_items,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    calls: list[tuple[str, int, float]] = []

    def generate(self, system_prompt, user_prompt, output_tokens, deadline_seconds):
        route = "profile" if "COMMITTED MARKDOWN NOTE:" in user_prompt else "source"
        calls.append((route, output_tokens, deadline_seconds))
        return _bundle_from_prompt(user_prompt, "built-in-profile")

    monkeypatch.setattr(DeepSeekReader, "_generate_text", generate)
    request = MapRequest(
        tmp_path, provider="deepseek", model="deepseek-v4-flash", allow_cloud=True
    )

    report = run_map(
        request, client=FakeZotero(sample_items[:1]), run_id="builtin-profile"
    )

    assert report.status == "completed"
    assert [route for route, _, _ in calls] == ["source"]
    assert isinstance(DeepSeekReader(allow_cloud=True), LiteratureReasoner)
    profile = _only_profile(tmp_path)
    assert profile.concepts == ["built-in-profile"]
    assert profile.context["profile_generation_route"] == "source_analysis_bundle"
    progress = _progress(tmp_path, "builtin-profile")
    assert progress["source_provider_call_count"] == 1
    assert progress["literature_provider_call_count"] == 0
    assert progress["provider_call_count"] == 1
    completed_status = get_status(tmp_path, "builtin-profile")
    assert completed_status.counts["source_provider_call_count"] == 1
    assert completed_status.counts["literature_provider_call_count"] == 0
    assert completed_status.counts["provider_call_count"] == 1


def test_build_map_constructs_configured_builtin_reasoner(
    tmp_path: Path,
    sample_items,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_report = run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1"),
        client=FakeZotero(sample_items),
        reader=FakeReader(),
        run_id="build-map-source",
    )
    reasoner = _ExplicitReasoner()
    constructed: list[tuple[str, str, bool]] = []

    def fake_provider(name: str, model: str, *, allow_cloud: bool):
        constructed.append((name, model, allow_cloud))
        return reasoner

    monkeypatch.setattr("auto_zettelkasten.api.provider_from_name", fake_provider)
    result = build_map(
        tmp_path,
        run_id="build-map-reasoner",
        source_set=source_report.source_set,
        provider="deepseek",
        model="deepseek-v4-flash",
        allow_cloud=True,
    )

    assert result.status == "built"
    assert constructed == [("deepseek", "deepseek-v4-flash", True)]
    assert reasoner.request_deadline == 600.0
    assert reasoner.profile_calls == 0
    progress = _progress(tmp_path, "build-map-reasoner")
    assert progress["status"] == "completed"
    assert progress["stage"] == "reporting"
    assert progress["inventory_count"] == source_report.inventory_count
    assert progress["validated_note_count"] == source_report.validated_note_count
    assert progress["limited_note_count"] == source_report.limited_note_count
    assert (
        progress["parked_for_review_count"]
        == source_report.parked_for_review_count
    )
    assert progress["pending_count"] == 0
    assert progress["profile_count"] == 2
    assert progress["literature_provider_call_count"] == progress[
        "synthesis_call_count"
    ]
    profile_hits = result.metadata["literature_map"]["profile_result"][
        "checkpoint_hits"
    ]
    assert (
        progress["checkpoint_hit_count"]
        == profile_hits + progress["synthesis_checkpoint_hit_count"]
    )


def test_cluster_failure_keeps_committed_reciprocal_atomic_relationships(
    tmp_path: Path,
    sample_items,
) -> None:
    report = run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1"),
        client=FakeZotero(sample_items),
        reader=FakeReader(),
        literature_reasoner=_RelationshipThenClusterFailureReasoner(),
        run_id="relationship-before-cluster-failure",
    )

    assert report.status == "partial"
    assert any(
        "cluster provider unavailable" in str(row.get("reason") or "")
        for row in report.errors
    )
    assert report.literature_map["synthesis_call_count"] == 3
    assert report.literature_map["synthesis_failure_count"] == 1
    registry = yaml.safe_load(
        (
            tmp_path / "02_source_memory" / "indexes" / "typed_links.yml"
        ).read_text()
    )
    substantive = [
        row for row in registry["links"] if row["relation_type"] == "complements"
    ]
    assert len(substantive) == 1
    note_paths = sorted(
        (tmp_path / "02_source_memory" / "notes").glob("*.md")
    )
    assert len(note_paths) == 2
    for path in note_paths:
        note = read_note(path)
        assert {
            row["relation_type"] for row in note["frontmatter"]["related_notes"]
        } >= {"complements"}
        assert "<!-- auto-zettelkasten:graph:start -->" in path.read_text()


def test_unchanged_workspace_replay_makes_no_new_relationship_or_cluster_calls(
    tmp_path: Path,
    sample_items,
) -> None:
    source_report = run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1"),
        client=FakeZotero(sample_items),
        reader=FakeReader(),
        run_id="relationship-replay-sources",
    )
    reasoner = _ReplayableRelationshipReasoner()
    first = build_map(
        tmp_path,
        run_id="relationship-replay",
        provider="ollama",
        model="fake-1",
        reasoner=reasoner,
        comparison_collection_keys=("C1", "C2"),
    )
    assert first.status == "built"
    before_calls = (
        reasoner.profile_calls,
        reasoner.candidate_calls,
        reasoner.adjudication_calls,
        reasoner.cluster_calls,
    )
    before_notes = {
        path: path.read_bytes()
        for path in (tmp_path / "02_source_memory" / "notes").glob("*.md")
    }
    before_files = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    replay = build_map(
        tmp_path,
        run_id="relationship-replay",
        provider="ollama",
        model="fake-1",
        reasoner=reasoner,
        comparison_collection_keys=("C1", "C2"),
        resume=True,
    )

    assert replay.status == "built"
    assert (
        reasoner.profile_calls,
        reasoner.candidate_calls,
        reasoner.adjudication_calls,
        reasoner.cluster_calls,
    ) == before_calls
    assert {
        path: path.read_bytes() for path in before_notes
    } == before_notes
    assert {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in tmp_path.rglob("*")
        if path.is_file()
    } == before_files
    assert source_report.validated_note_count == len(before_notes)


def test_reasoner_replay_reuses_same_provider_profiles_created_by_deterministic_route(
    tmp_path: Path,
    sample_items,
) -> None:
    source_report = run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1"),
        client=FakeZotero(sample_items),
        reader=FakeReader(),
        run_id="route-aware-source",
    )
    build_map(
        tmp_path,
        run_id="route-aware-seed",
        source_set=source_report.source_set,
        provider="deepseek",
        model="deepseek-v4-flash",
        allow_cloud=False,
        literature_policy=LiteratureMappingPolicy(max_synthesis_calls=2),
    )
    reasoner = _ExplicitReasoner()

    replay = build_map(
        tmp_path,
        run_id="route-aware-replay",
        source_set=source_report.source_set,
        provider="deepseek",
        model="deepseek-v4-flash",
        allow_cloud=True,
        reasoner=reasoner,
        resume=True,
    )

    profile_result = replay.metadata["literature_map"]["profile_result"]
    assert reasoner.profile_calls == 0
    assert profile_result["provider_calls"] == 0
    assert profile_result["checkpoint_hits"] == len(sample_items)
    for sidecar in (tmp_path / "02_source_memory" / "profiles").glob("*.yml"):
        stored = yaml.safe_load(sidecar.read_text())["profile"]
        assert stored["context"]["profile_generation_route"] == "deterministic"
        assert (
            stored["context"]["reasoner_identity"]
            == "auto_zettelkasten.profiles.deterministic_profile:v1"
        )


def test_primary_run_honors_synthesis_disabled_without_profile_or_map_artifacts(
    tmp_path: Path,
    sample_items,
) -> None:
    reader = _SourceOnlyDeepSeek()
    report = run_map(
        MapRequest(
            tmp_path,
            provider="deepseek",
            model="deepseek-v4-flash",
            allow_cloud=True,
            literature_policy=LiteratureMappingPolicy(synthesis_enabled=False),
        ),
        client=FakeZotero(sample_items[:1]),
        reader=reader,
        run_id="synthesis-disabled",
    )

    assert report.status == "completed"
    assert reader.calls == 1
    assert report.profile_count == 0
    assert report.cluster_map["status"] == "synthesis_disabled"
    assert report.gap_map["status"] == "synthesis_disabled"
    assert not list((tmp_path / "02_source_memory" / "profiles").glob("*.yml"))
    assert not (tmp_path / "03_literature_synthesis" / "manifest.yml").exists()


def test_legacy_profile_reasoner_is_not_called_after_source_generation(
    tmp_path: Path, sample_items
) -> None:
    reasoner = _ConcurrentReasoner()
    report = run_map(
        MapRequest(
            tmp_path,
            provider="ollama",
            model="fake-1",
            literature_policy=LiteratureMappingPolicy(profile_workers=2),
        ),
        client=FakeZotero(sample_items),
        reader=FakeReader(),
        literature_reasoner=reasoner,
        run_id="parallel-profiles",
    )

    assert report.status == "completed"
    assert reasoner.profile_calls == 0
    assert reasoner.max_active_calls == 0
    assert report.literature_provider_call_count == report.synthesis_call_count
    assert report.synthesis_call_count >= 1


def test_profile_call_budget_caps_source_generation_without_profile_calls(
    tmp_path: Path,
    sample_items,
) -> None:
    reasoner = _ConcurrentReasoner()
    report = run_map(
        MapRequest(
            tmp_path,
            provider="ollama",
            model="fake-1",
            literature_policy=LiteratureMappingPolicy(
                max_profile_calls=1, profile_workers=2
            ),
        ),
        client=FakeZotero(sample_items),
        reader=FakeReader(),
        literature_reasoner=reasoner,
        run_id="profile-budget",
    )

    assert report.status == "completed_with_parked_items"
    assert report.validated_note_count == 1
    assert report.parked_for_review_count == 1
    assert report.source_provider_call_count == 1
    assert reasoner.profile_calls == 0


def test_legacy_profile_failure_cannot_park_source_results(
    tmp_path: Path,
    sample_items,
) -> None:
    request = MapRequest(tmp_path, provider="ollama", model="fake-1")
    first_reasoner = _RecoveringReasoner({"ITEMA"})

    first = run_map(
        request,
        client=FakeZotero(sample_items),
        reader=FakeReader(),
        literature_reasoner=first_reasoner,
        run_id="profile-recovery",
    )

    assert first.status == "completed"
    assert first.profile_count == 2
    assert first.profile_valid_count == 2
    assert first.literature_failure_count == 0
    assert first_reasoner.profile_calls == 0
    failure_dir = (
        tmp_path
        / "11_state"
        / "runs"
        / "profile-recovery"
        / "literature"
        / "profile_failures"
    )
    assert not list(failure_dir.glob("*.yml"))

    recovered_reasoner = _RecoveringReasoner()
    recovered = resume_map(
        tmp_path,
        "profile-recovery",
        client=FakeZotero([]),
        reader=FakeReader(),
        literature_reasoner=recovered_reasoner,
    )

    assert recovered.status == "completed"
    assert recovered.profile_count == 2
    assert recovered.profile_valid_count == 2
    assert recovered.literature_failure_count == 0
    assert recovered_reasoner.profile_calls == 0
    assert not list(failure_dir.glob("*.yml"))


def test_contract_invalid_legacy_profile_method_is_not_used(
    tmp_path: Path,
    sample_items,
) -> None:
    reasoner = _ContractInvalidReasoner()
    first = run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1"),
        client=FakeZotero(sample_items[:1]),
        reader=FakeReader(),
        literature_reasoner=reasoner,
        run_id="profile-contract-fallback",
    )

    assert first.status == "completed"
    assert first.profile_count == 1
    assert first.profile_valid_count == 1
    assert first.profile_excluded_count == 0
    assert first.literature_failure_count == 0
    assert reasoner.profile_calls == 0
    profile = _only_profile(tmp_path)
    assert profile.excluded_from_synthesis is False
    assert profile.context["profile_generation_route"] == "deterministic"

    replay_reasoner = _RecoveringReasoner()
    replay = build_map(
        tmp_path,
        run_id="profile-contract-fallback-replay",
        source_set=first.source_set,
        provider="ollama",
        model="fake-1",
        reasoner=replay_reasoner,
    )

    assert replay.status == "built"
    assert replay_reasoner.profile_calls == 0
    recovered = _only_profile(tmp_path)
    assert recovered.excluded_from_synthesis is False
    assert recovered.context.get("lazy_reprofile_required") is not True


def test_resume_keeps_frozen_source_set_identity_when_a_parked_source_recovers(
    tmp_path: Path,
    sample_items,
) -> None:
    request = MapRequest(tmp_path, provider="ollama", model="fake-1")
    first_reasoner = _RecoveringReasoner()
    first = run_map(
        request,
        client=FakeZotero(sample_items),
        reader=_RecoveringSourceReader({"ITEMA"}),
        literature_reasoner=first_reasoner,
        run_id="source-recovery",
    )

    assert first.validated_note_count == 1
    assert first.parked_for_review_count == 1
    assert first_reasoner.profile_calls == 0

    resumed_reasoner = _RecoveringReasoner()
    resumed = resume_map(
        tmp_path,
        "source-recovery",
        client=FakeZotero([]),
        reader=_RecoveringSourceReader(),
        literature_reasoner=resumed_reasoner,
        retry_terminal_failures=True,
    )

    assert resumed.validated_note_count == 2
    assert resumed.parked_for_review_count == 0
    assert resumed.source_set_id == first.source_set_id
    assert resumed_reasoner.profile_calls == 0


def test_profile_content_remains_source_owned_across_collection_maps(
    tmp_path: Path,
    sample_items,
) -> None:
    first_reasoner = _RecoveringReasoner()
    first = run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1"),
        client=FakeZotero(sample_items[:1]),
        reader=FakeReader(),
        literature_reasoner=first_reasoner,
        run_id="profile-lineage-first",
    )
    changed_source_set = {
        **first.source_set,
        "source_set_id": "source-set-zotero-other-snapshot",
        "dependency_hash": "other-source-set-dependency",
    }
    rebased_reasoner = _RecoveringReasoner()

    manifest = build_map(
        tmp_path,
        run_id="profile-lineage-rebase",
        source_set=changed_source_set,
        provider="ollama",
        model="fake-1",
        reasoner=rebased_reasoner,
    )

    assert manifest.status == "built"
    assert rebased_reasoner.profile_calls == 0
    profile = _only_profile(tmp_path)
    assert "source_set_id" not in profile.context


def test_current_mechanical_profile_reuses_unchanged_inspected_source_content(
    tmp_path: Path,
    sample_items,
) -> None:
    first_reasoner = _RecoveringReasoner()
    first = run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1"),
        client=FakeZotero(sample_items[:1]),
        reader=FakeReader(),
        literature_reasoner=first_reasoner,
        run_id="mechanical-source-lineage-first",
    )
    profile = _only_profile(tmp_path)
    profile.note_hash = "stale-projection-hash"
    profile.dependency_hash = "stale-dependency"
    profile.context.update(
        {
            "profile_generation_route": "mechanical_legacy_upgrade",
            "reasoner_identity": "auto_zettelkasten.profiles.legacy_upgrade:v4",
        }
    )
    profile.validity.update(
        {
            "profile_prompt_version": "6",
            "classifier_version": "3",
                "algorithm_version": "5",
            "legacy_profile_upgraded_mechanically": True,
        }
    )
    save_profile(tmp_path / "02_source_memory" / "profiles", profile)
    replay_reasoner = _RecoveringReasoner({"ITEMA"})

    manifest = build_map(
        tmp_path,
        run_id="mechanical-source-lineage-reuse",
        source_set=first.source_set,
        provider="ollama",
        model="fake-1",
        reasoner=replay_reasoner,
    )

    assert manifest.status == "built"
    assert replay_reasoner.profile_calls == 0
    reused = _only_profile(tmp_path)
    assert reused.context["profile_reuse_basis"] == (
        "unchanged_inspected_source_content"
    )


def test_explicit_reasoner_takes_precedence_over_builtin_reader(
    tmp_path: Path,
    sample_items,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    built_in_routes: list[str] = []

    def generate(self, system_prompt, user_prompt, output_tokens, deadline_seconds):
        route = "profile" if "COMMITTED MARKDOWN NOTE:" in user_prompt else "source"
        built_in_routes.append(route)
        if route == "profile":
            raise AssertionError(
                "the built-in profile route must not replace an explicit reasoner"
            )
        return _bundle_from_prompt(user_prompt, "source-bundle-profile")

    monkeypatch.setattr(DeepSeekReader, "_generate_text", generate)
    reasoner = _ExplicitReasoner()
    report = run_map(
        MapRequest(
            tmp_path, provider="deepseek", model="deepseek-v4-flash", allow_cloud=True
        ),
        client=FakeZotero(sample_items[:1]),
        reader=DeepSeekReader(allow_cloud=True),
        literature_reasoner=reasoner,
        run_id="explicit-profile",
    )

    assert report.status == "completed"
    assert built_in_routes == ["source"]
    assert reasoner.profile_calls == 0
    profile = _only_profile(tmp_path)
    assert profile.concepts == ["source-bundle-profile"]
    assert profile.context["profile_generation_route"] == "source_analysis_bundle"


def test_limited_note_uses_deterministic_profile_without_builtin_call(
    tmp_path: Path,
    sample_items,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    def unexpected_generate(*args, **kwargs):
        raise AssertionError(
            "limited notes must not call the source reader or profile reasoner"
        )

    monkeypatch.setattr(DeepSeekReader, "_generate_text", unexpected_generate)
    report = run_map(
        MapRequest(
            tmp_path, provider="deepseek", model="deepseek-v4-flash", allow_cloud=True
        ),
        client=FakeZotero(sample_items[:1], missing={"ITEMA"}),
        run_id="limited-profile",
    )

    assert report.limited_note_count == 1
    profile = _only_profile(tmp_path)
    assert profile.excluded_from_synthesis is True
    assert profile.context["profile_generation_route"] == "deterministic"
    progress = _progress(tmp_path, "limited-profile")
    assert progress["source_provider_call_count"] == 0
    assert progress["literature_provider_call_count"] == 0
    assert progress["provider_call_count"] == 0


def test_builtin_route_reuses_current_deterministic_profile_then_replay_is_zero_call(
    tmp_path: Path,
    sample_items,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    request = MapRequest(
        tmp_path, provider="deepseek", model="deepseek-v4-flash", allow_cloud=True
    )
    source_reader = _SourceOnlyDeepSeek()
    first = run_map(
        request,
        client=FakeZotero(sample_items[:1]),
        reader=source_reader,
        run_id="route-invalidation",
    )
    assert first.status == "completed"
    assert source_reader.calls == 1

    note_path = tmp_path / first.items[0]["note_path"]
    legacy_fingerprint = profile_dependency_fingerprint(
        note_path.read_text(encoding="utf-8"),
        source_set_id=first.source_set_id,
        provider=request.provider,
        model=request.model,
        policy=request.literature_policy,
    )
    sidecar_path = next((tmp_path / "02_source_memory" / "profiles").glob("*.yml"))
    sidecar_payload = yaml.safe_load(sidecar_path.read_text(encoding="utf-8"))
    sidecar_payload["profile"]["dependency_hash"] = legacy_fingerprint
    sidecar_path.write_text(
        yaml.safe_dump(sidecar_payload, sort_keys=False), encoding="utf-8"
    )
    checkpoint_path = next(
        (
            tmp_path
            / "11_state"
            / "runs"
            / "route-invalidation"
            / "literature"
            / "profile_calls"
        ).glob("*.yml")
    )
    checkpoint_payload = yaml.safe_load(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint_payload["fingerprint"] = legacy_fingerprint
    checkpoint_payload["profile"]["dependency_hash"] = legacy_fingerprint
    checkpoint_path.write_text(
        yaml.safe_dump(checkpoint_payload, sort_keys=False), encoding="utf-8"
    )

    calls: list[str] = []

    def generate(self, system_prompt, user_prompt, output_tokens, deadline_seconds):
        route = "profile" if "COMMITTED MARKDOWN NOTE:" in user_prompt else "source"
        calls.append(route)
        if route == "profile":
            return _profile_from_prompt(user_prompt, "replacement-profile")
        return _analysis()

    monkeypatch.setattr(DeepSeekReader, "_generate_text", generate)
    reused = resume_map(tmp_path, "route-invalidation", client=FakeZotero([]))
    assert reused.status == "completed"
    assert calls == []
    assert (
        _only_profile(tmp_path).context["profile_generation_route"] == "deterministic"
    )
    reused_progress = _progress(tmp_path, "route-invalidation")
    assert reused_progress["source_provider_call_count"] == 0
    assert reused_progress["literature_provider_call_count"] == 0
    assert reused_progress["provider_call_count"] == 0
    assert reused_progress["checkpoint_hit_count"] >= 1

    # Simulate a v1.2 checkpoint that was excluded by the older
    # document-level quantitative gate. Exact checkpoint hits must still pass
    # through the mechanical revalidation path on replay.
    sidecar_payload = yaml.safe_load(sidecar_path.read_text(encoding="utf-8"))
    sidecar_payload["profile"]["excluded_from_synthesis"] = True
    sidecar_payload["profile"]["exclusion_reason"] = (
        "profile_or_note_validation_failed:anchor_0:typed_quantitative_result_required"
    )
    sidecar_path.write_text(
        yaml.safe_dump(sidecar_payload, sort_keys=False), encoding="utf-8"
    )
    checkpoint_payload = yaml.safe_load(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint_payload["profile"]["excluded_from_synthesis"] = True
    checkpoint_payload["profile"]["exclusion_reason"] = (
        "profile_or_note_validation_failed:anchor_0:typed_quantitative_result_required"
    )
    checkpoint_path.write_text(
        yaml.safe_dump(checkpoint_payload, sort_keys=False), encoding="utf-8"
    )

    calls.clear()
    replayed = resume_map(tmp_path, "route-invalidation", client=FakeZotero([]))
    assert replayed.status == "completed"
    assert calls == []
    assert _only_profile(tmp_path).excluded_from_synthesis is False
    replay_progress = _progress(tmp_path, "route-invalidation")
    assert replay_progress["source_provider_call_count"] == 0
    assert replay_progress["literature_provider_call_count"] == 0
    assert replay_progress["provider_call_count"] == 0
    assert replay_progress["checkpoint_hit_count"] >= 1


@pytest.mark.parametrize(
    "reader",
    [
        DeepSeekReader(allow_cloud=False),
        OpenRouterReader("openai/gpt-4.1-mini", allow_cloud=False),
        GeminiReader(allow_cloud=False),
    ],
)
def test_cloud_builtin_profile_route_requires_consent(reader) -> None:
    with pytest.raises(CloudPermissionError, match="explicit allow_cloud"):
        reader.profile_source({"profile_prompt": "private committed note"})


def test_builtin_profile_prompt_v3_requires_typed_lineage_locators_and_quantitative_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    captured: dict[str, str] = {}

    def generate(self, system_prompt, user_prompt, output_tokens, deadline_seconds):
        captured["system"] = system_prompt
        captured["user"] = user_prompt
        return {}

    monkeypatch.setattr(DeepSeekReader, "_generate_text", generate)
    reader = DeepSeekReader(allow_cloud=True)

    assert (
        reader.profile_source(
            {"profile_prompt": "committed note only"},
            context={"profile_prompt_version": "6"},
        )
        == {}
    )
    assert "profile prompt v6" in captured["system"]
    assert "source_locators" in captured["system"]
    assert "quantitative_result" in captured["system"]
    assert "study_lineage" in captured["system"]
    assert captured["user"] == "committed note only"
    with pytest.raises(ProviderError, match="unsupported profile prompt version: 2"):
        reader.profile_source(
            {"profile_prompt": "committed note only"},
            context={"profile_prompt_version": "2"},
        )


def test_all_builtin_readers_expose_complete_literature_reasoner_protocol() -> None:
    readers = [
        DeepSeekReader(),
        OpenRouterReader("openai/gpt-4.1-mini"),
        GeminiReader(),
        OllamaReader(),
    ]
    assert all(isinstance(reader, LiteratureReasoner) for reader in readers)
    for reader in readers:
        assert callable(reader.propose_clusters)
        assert callable(reader.map_debates)
        assert callable(reader.synthesize_cluster)
        assert callable(reader.detect_gaps)


def test_builtin_reader_executes_typed_collection_reasoning_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    responses = {
        "collection-clustering": {"clusters": []},
        "debate-mapping": {"assessments": []},
        "full-note cluster writer": {
            "cluster_id": "cluster-1",
            "status": "accepted",
            "title": "Mediator legitimacy",
            "organizing_mode": "question",
            "organizing_problem": "How does legitimacy shape settlement durability?",
            "bottom_line": "Legitimacy is associated with durability in the supplied scope.",
            "lines_of_inquiry": [
                {
                    "title": "Legitimacy and durability",
                    "synthesis": "The source reports a bounded association.",
                    "study_findings": [
                        {
                            "source_id": "source-a",
                            "finding": "A source-specific result remains relevant.",
                            "method_scope": "Comparative case study.",
                            "relation_to_line": "supports",
                            "evidence": [
                                {
                                    "source_id": "source-a",
                                    "evidence_anchor_id": "claim-a",
                                    "locator": "p. 10",
                                }
                            ],
                        }
                    ],
                }
            ],
            "differences": [],
            "limits": [
                "Temporal: 2000-2020",
                "Regional: African civil wars",
            ],
            "related_clusters": [],
            "retained_member_ids": ["source-a"],
            "dropped_members": [],
            "missing_member_ids": [],
        },
        "collection-gap": {"gaps": [], "rejected": []},
    }
    calls: list[str] = []
    prompts: dict[str, str] = {}
    system_prompts: dict[str, str] = {}
    output_caps: dict[str, int] = {}
    reasoning_efforts: dict[str, str | None] = {}

    def generate(self, system_prompt, user_prompt, output_tokens, deadline_seconds):
        route = next(key for key in responses if key in system_prompt)
        calls.append(route)
        prompts[route] = user_prompt
        system_prompts[route] = system_prompt
        output_caps[route] = output_tokens
        reasoning_efforts[route] = reader_module._REASONING_EFFORT.get()
        return responses[route]

    monkeypatch.setattr(DeepSeekReader, "_generate_text", generate)
    reader = DeepSeekReader(allow_cloud=True)
    request = LiteratureMapRequest(
        workspace=".", provider="deepseek", model="deepseek-v4-flash", allow_cloud=True
    )
    clustering_profile = EvidenceProfile(
        source_id="source-a",
        note_id="note-a",
        study_family_id="family-a",
        concepts=["mediator legitimacy"],
        future_research=["unused-detail-must-not-enter-clustering-packet"],
        findings=[
            {
                "finding_id": "claim-a",
                "claim": "Legitimacy matters.",
                "locator": "p. 10",
            }
        ],
    )
    raw_profile = clustering_profile.to_dict()
    normalized_profile = {
        "source_id": raw_profile["source_id"],
        "note_id": raw_profile["note_id"],
        "title": "Mediator legitimacy and settlement durability",
        "study_family_id": raw_profile["study_family_id"],
        "analytical": True,
        "semantic_topic_scores": {"legitimacy mediator": 0.9},
        "semantic_topic_labels": {"legitimacy mediator": "mediator legitimacy"},
        "dimensions": {
            "theory": ["legitimacy theory"],
            "mechanism": ["acceptance"],
            "method": ["comparative case study"],
            "data": ["peace agreements"],
            "case": ["African civil wars"],
            "period": ["1990-2020"],
            "outcome": ["settlement durability"],
        },
        "claims": [
            {
                "claim_id": "claim-a",
                "text": "Legitimacy matters.",
                "locator": "p. 10",
                "direction": "positive",
                "boundary_condition": "African civil wars",
                "dimensions": {"outcome": ["settlement durability"]},
                "source_locators": [
                    {
                        "locator_id": "locator-a",
                        "source_id": "source-a",
                        "evidence_anchor_id": "claim-a",
                        "locator_type": "page",
                        "value": "p. 10",
                        "page_start": 10,
                        "page_end": 10,
                        "source_native": True,
                        "supports_strong_assertion": True,
                    }
                ],
                "quantitative_result": None,
            }
        ],
        "study_lineage": {
            "study_lineage_id": "lineage-a",
            "source_ids": ["source-a"],
            "authors": ["Researcher A"],
            "institutions": [],
            "datasets": ["peace agreements"],
            "data_sources": ["peace agreements"],
            "sampling_frame": "",
            "unit_of_analysis": "mediation episode",
            "populations": ["African civil wars"],
            "periods": ["1990-2020"],
            "publication_relationships": [],
            "institutional_series": [],
            "overlap_signals": ["dataset:peace agreements"],
            "confidence": "moderate",
        },
    }
    limited_profile = {
        **normalized_profile,
        "source_id": "source-limited",
        "note_id": "note-limited",
        "title": "limited-profile-must-not-enter-clustering-packet",
        "analytical": False,
    }
    assert reader.propose_clusters([normalized_profile, limited_profile], request) == {
        "clusters": []
    }
    assert "claim-a" in prompts["collection-clustering"]
    assert (
        "Mediator legitimacy and settlement durability"
        in prompts["collection-clustering"]
    )
    assert "settlement durability" in prompts["collection-clustering"]
    assert "study_lineage" in prompts["collection-clustering"]
    assert "source_locators" in prompts["collection-clustering"]
    assert "cluster prompt v17" in system_prompts["collection-clustering"]
    assert "family_relation" in system_prompts["collection-clustering"]
    assert "independence_assessments" in system_prompts["collection-clustering"]
    assert (
        "limited-profile-must-not-enter-clustering-packet"
        not in prompts["collection-clustering"]
    )
    assert (
        "unused-detail-must-not-enter-clustering-packet"
        not in prompts["collection-clustering"]
    )
    assert output_caps["collection-clustering"] == 64_000
    assert reasoning_efforts["collection-clustering"] == "medium"
    unrelated_profile = {
        **normalized_profile,
        "source_id": "source-unrelated",
        "note_id": "note-unrelated",
        "title": "unrelated-profile-must-not-enter-repair-packet",
    }
    assert reader.propose_clusters(
        [normalized_profile, unrelated_profile],
        request,
        context={
            "coverage_repair_source_ids": ["source-a"],
            "prior_proposals": [],
        },
    ) == {"clusters": []}
    assert "source-a" in prompts["collection-clustering"]
    assert (
        "unrelated-profile-must-not-enter-repair-packet"
        not in prompts["collection-clustering"]
    )
    assert "semantically connected whole-profile component" in prompts[
        "collection-clustering"
    ]
    assert output_caps["collection-clustering"] == 24_000
    assert reader.propose_clusters(
        [normalized_profile, unrelated_profile],
        request,
        context={
            "coverage_repair_source_ids": ["source-a"],
            "coverage_component_source_ids": ["source-a"],
            "coverage_audit_mode": "collection",
            "coverage_candidate_components": [
                {
                    "focus_source_ids": ["source-a"],
                    "source_ids": ["source-a"],
                }
            ],
            "prior_proposals": [],
        },
    ) == {"clusters": []}
    assert "one best locator-backed anchor per core source" in prompts[
        "collection-clustering"
    ]
    assert "coverage_candidate_components" in prompts["collection-clustering"]
    assert output_caps["collection-clustering"] == 64_000
    assert reader.map_debates([], request) == {"assessments": []}
    synthesis = reader.synthesize_cluster([], request)
    assert output_caps["full-note cluster writer"] == 128_000
    assert synthesis["cluster_id"] == "cluster-1"
    assert synthesis["limits"] == [
        "Temporal: 2000-2020",
        "Regional: African civil wars",
    ]
    assert synthesis["cluster_contract"] == "streamlined-full-note-v1"
    assert synthesis["lines_of_inquiry"][0]["study_findings"][0]["source_id"] == "source-a"
    assert "cluster synthesis prompt v31" in system_prompts["full-note cluster writer"]
    assert "Read every supplied atomic_note_markdown" in system_prompts[
        "full-note cluster writer"
    ]
    assert "Every retained member" in system_prompts["full-note cluster writer"]
    assert "A partial-document member may be retained" in system_prompts[
        "full-note cluster writer"
    ]
    assert "do not present unavailable findings as empirical support" in system_prompts[
        "full-note cluster writer"
    ]
    assert "not generic thematic boilerplate" in system_prompts[
        "full-note cluster writer"
    ]
    assert "below 7,500 output tokens" not in system_prompts[
        "full-note cluster writer"
    ]
    assert "read every complete atomic note" in prompts[
        "full-note cluster writer"
    ].casefold()
    reader.synthesize_cluster(
        [],
        request,
        context={
            "response_budget": {
                "max_output_tokens": 4_500,
                "max_evidence_threads": 3,
                "source_contributions_per_core": 1,
                "max_central_findings": 3,
                "max_items_per_optional_section": 2,
                "max_gap_hypotheses": 2,
            }
        },
    )
    assert "below 4500 output tokens" not in prompts["full-note cluster writer"]
    gap_context = {
        "clusters": [{"cluster_id": "cluster-1", "label": "Mediator legitimacy"}],
        "cluster_syntheses": {
            "cluster-1": {
                "cluster_id": "cluster-1",
                "central_findings": [
                    {
                        "finding": "Legitimacy matters.",
                        "evidence": [
                            {
                                "source_id": "source-a",
                                "claim_id": "claim-a",
                                "locator": "p. 10",
                            }
                        ],
                    }
                ],
            }
        },
        "candidates": [
            {
                "gap_id": "gap-1",
                "rule": "empirical_coverage",
                "topic": "mediator legitimacy",
                "precise_missing_evidence": "Comparable tests outside African civil wars",
                "supporting_evidence": [
                    {"source_id": "source-a", "claim_id": "claim-a", "locator": "p. 10"}
                ],
                "internal_search_results": [
                    {
                        "source_id": f"source-{index}",
                        "status": "relevant_not_answering",
                        "semantic_overlap": ["legitimacy"],
                    }
                    for index in range(20)
                ],
                "promotion_metadata": {
                    "redundant-promotion-detail-must-not-enter-gap-packet": "x"
                },
            }
        ],
        "internal_search_log": [
            {
                "gap_id": "gap-1",
                "analytical_profile_count_searched": 65,
                "complete": True,
            }
        ],
    }
    assert reader.detect_gaps([normalized_profile], request, context=gap_context) == {
        "gaps": [],
        "rejected": [],
    }
    assert "claim-a" in prompts["collection-gap"]
    assert "Comparable tests outside African civil wars" in prompts["collection-gap"]
    assert (
        "redundant-promotion-detail-must-not-enter-gap-packet"
        not in prompts["collection-gap"]
    )
    assert output_caps["collection-gap"] == 32_000
    assert "FINAL GAP REQUIREMENTS" in prompts["collection-gap"]
    assert "Do not invent follow-up years" in prompts["collection-gap"]
    assert (
        "generic observation that an observational association lacks causal identification"
        in prompts["collection-gap"]
    )
    assert calls == [
        "collection-clustering",
        "collection-clustering",
        "collection-clustering",
        "debate-mapping",
        "full-note cluster writer",
        "full-note cluster writer",
        "collection-gap",
    ]
