from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from auto_zettelkasten.files import read_yaml
from auto_zettelkasten.literature import (
    _CheckpointedReasonerCalls,
    _cluster_acquisition_revision,
    _persist_cluster_acquisition_revisions,
    _schedule_cluster_writers,
    _synthesis_failure_class,
    build_literature_report,
    validate_streamlined_cluster_synthesis,
)
from auto_zettelkasten.models import LiteratureMapRequest, LiteratureMappingPolicy


def _profile(source_id: str) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "note_id": f"note-{source_id}",
        "note_status": "analytical_atomic_note",
        "evidence_eligibility": "substantive_bounded",
        "title": f"Source {source_id}",
        "thesis": f"Thesis {source_id}",
        "methods": ["case comparison"],
        "study_family_id": source_id,
        "evidence_anchors": [
            {
                "evidence_anchor_id": f"anchor-{source_id}",
                "source_id": source_id,
                "claim": f"Finding {source_id}",
                "locator": "p. 4",
                "planning_roles": ["major_finding"],
                "salience_priority": 10,
                "support_envelope": {
                    "coverage": "full_text",
                    "support_status": "supported",
                    "empirical_role": "descriptive",
                    "argument_role": "none",
                },
            }
        ],
    }


def _cluster_response(
    cluster: Mapping[str, Any],
    profiles: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    source_ids = [str(row["source_id"]) for row in profiles]
    return {
        "cluster_contract": "streamlined-full-note-v2",
        "cluster_id": str(cluster["cluster_id"]),
        "status": "accepted",
        "title": "Shared problem",
        "organizing_mode": "question",
        "organizing_problem": "How do the studies address the problem?",
        "bottom_line": "They identify distinct parts of one bounded problem.",
        "retained_member_ids": source_ids,
        "dropped_members": [],
        "material_exclusions": [],
        "differences": [],
        "limits": [],
        "related_clusters": [],
        "lines_of_inquiry": [
            {
                "title": "Main line",
                "synthesis": "Both studies contribute relevant findings.",
                "study_findings": [
                    {
                        "source_id": source_id,
                        "finding": f"{source_id} contributes a finding.",
                        "method_scope": "Case comparison.",
                        "relation_to_line": "contributes",
                        "evidence": [
                            {
                                "source_id": source_id,
                                "evidence_anchor_id": f"anchor-{source_id}",
                                "locator": "p. 4",
                            }
                        ],
                    }
                    for source_id in source_ids
                ],
            }
        ],
    }


def test_v2_acquisition_dispositions_derive_visible_recommendations() -> None:
    profiles = [_profile("A"), _profile("B")]
    cluster = {"cluster_id": "cluster-one", "source_ids": ["A", "B"]}
    response = _cluster_response(cluster, profiles)
    response["acquisition_candidate_dispositions"] = [
        {
            "external_source_id": "mapped-later",
            "decision": "recommend",
            "why_it_matters": "It defines the shared mechanism.",
            "selected_attribution_ids": ["position-A"],
        },
        {
            "external_source_id": "secondary",
            "decision": "relevant_secondary",
        },
    ]
    validated = validate_streamlined_cluster_synthesis(
        response,
        cluster,
        profiles,
        important_unmapped_literature=[
            {
                "external_source_id": "mapped-later",
                "raw_citation": "Mapped Later 2000",
                "status": "known_zotero_unmapped",
                "attributions": [
                    {
                        "literature_position_id": "position-A",
                        "current_source_id": "A",
                        "characterization": "A treats it as foundational.",
                        "locator": "p. 3",
                    }
                ],
            },
            {
                "external_source_id": "secondary",
                "raw_citation": "Secondary 2001",
                "status": "not_in_snapshot",
                "attributions": [
                    {
                        "literature_position_id": "position-B",
                        "current_source_id": "B",
                        "characterization": "B uses it for context.",
                        "locator": "p. 5",
                    }
                ],
            },
        ],
    )

    assert validated["status"] == "reasoned"
    assert [
        row["external_source_id"]
        for row in validated["important_cited_works_not_yet_mapped"]
    ] == ["mapped-later"]
    assert validated["important_cited_works_not_yet_mapped"][0]["action"] == (
        "map_existing"
    )
    assert {
        row["decision"] for row in validated["acquisition_candidate_dispositions"]
    } == {
        "recommend",
        "relevant_secondary",
    }


def test_acquisition_contract_defects_do_not_park_good_cluster() -> None:
    profiles = [_profile("A"), _profile("B")]
    cluster = {"cluster_id": "cluster-one", "source_ids": ["A", "B"]}
    response = _cluster_response(cluster, profiles)
    response["acquisition_candidate_dispositions"] = [
        {
            "external_source_id": "missing-1",
            "decision": "recommend",
            "why_it_matters": "",
        },
        {
            "external_source_id": "unknown",
            "decision": "recommend",
            "why_it_matters": "Unknown.",
        },
    ]
    validated = validate_streamlined_cluster_synthesis(
        response,
        cluster,
        profiles,
        important_unmapped_literature=[
            {
                "external_source_id": "missing-1",
                "raw_citation": "Missing 2000",
                "status": "not_in_snapshot",
                "attributions": [
                    {
                        "literature_position_id": "position-A",
                        "current_source_id": "A",
                    }
                ],
            },
            {
                "external_source_id": "omitted",
                "raw_citation": "Omitted 2001",
                "status": "not_in_snapshot",
                "attributions": [
                    {
                        "literature_position_id": "position-B",
                        "current_source_id": "B",
                    }
                ],
            },
        ],
    )

    assert validated["status"] == "reasoned"
    assert validated["important_cited_works_not_yet_mapped"] == []
    assert {
        row["decision"] for row in validated["acquisition_candidate_dispositions"]
    } == {
        "unassessed_invalid_recommendation",
        "unassessed_response_omission",
    }


def test_partial_cluster_cannot_publish_parsed_acquisition_recommendation() -> None:
    profiles = [_profile("A"), _profile("B")]
    cluster = {
        "cluster_id": "cluster-one",
        "revision_hash": "revision-one",
        "source_ids": ["A", "B"],
    }
    response = _cluster_response(cluster, profiles)
    response["bottom_line"] = ""
    response["acquisition_candidate_dispositions"] = [
        {
            "external_source_id": "missing-1",
            "decision": "recommend",
            "why_it_matters": "It looks important but the cluster is invalid.",
        }
    ]
    candidates = [
        {
            "external_source_id": "missing-1",
            "raw_citation": "Missing 2000",
            "status": "not_in_snapshot",
            "attributions": [
                {
                    "literature_position_id": "position-A",
                    "current_source_id": "A",
                }
            ],
        }
    ]
    validated = validate_streamlined_cluster_synthesis(
        response,
        cluster,
        profiles,
        important_unmapped_literature=candidates,
    )
    revision = _cluster_acquisition_revision(
        cluster,
        candidates,
        state="failure",
        dispositions=validated["acquisition_candidate_dispositions"],
    )

    assert validated["status"] == "partial"
    assert revision["candidates"][0]["writer_disposition"] == (
        "unassessed_writer_failure"
    )


def test_cluster_acquisition_receipt_exists_before_writer_and_finishes_active(
    tmp_path: Path,
) -> None:
    profiles = [_profile("A"), _profile("B")]
    ledger_path = tmp_path / "03_literature_synthesis/cluster_acquisition_ledger.yml"

    class Reasoner:
        name = "local"
        model = "test"

        def synthesize_cluster(self, projected, request, *, context=None):
            ledger = read_yaml(ledger_path)
            assert ledger["revisions"][0]["state"] == "pending"
            response = _cluster_response(context["cluster"], projected)
            response["acquisition_candidate_dispositions"] = [
                {
                    "external_source_id": "missing-1",
                    "decision": "recommend",
                    "why_it_matters": "It supplies the foundational mechanism.",
                }
            ]
            return response

    report = build_literature_report(
        profiles,
        reasoner=Reasoner(),
        request=LiteratureMapRequest(tmp_path),
        source_notes=[
            {
                "source_id": source_id,
                "title": f"Source {source_id}",
                "source_scope": "full_document",
                "body": f"# Source {source_id}\n\nComplete note.",
            }
            for source_id in ("A", "B")
        ],
        shared_literature_plan={
            "literature_families": [
                {
                    "family_id": "shared",
                    "label": "Shared problem",
                    "organizing_problem": "How do the studies address the problem?",
                    "source_ids": ["A", "B"],
                    "proposed_roles": {"A": "core", "B": "supporting"},
                    "candidate_cluster": True,
                }
            ],
            "discovery_jobs": [],
            "neighboring_families": [],
        },
        literature_positions=[
            {
                "literature_position_id": "position-A",
                "current_source_id": "A",
                "raw_citation": "Missing 2000",
                "engagement": "A treats it as foundational.",
                "locator": "p. 3",
            }
        ],
        missing_sources=[
            {
                "external_source_id": "missing-1",
                "raw_citation": "Missing 2000",
                "discussed_by_source_ids": ["A"],
                "match_status": "not_in_snapshot",
            }
        ],
        acquisition_ledger_path=ledger_path,
    )

    ledger = read_yaml(ledger_path)
    assert report["packet"]["status"] == "complete"
    assert ledger["revisions"][0]["state"] == "active"
    assert ledger["revisions"][0]["candidates"][0]["writer_disposition"] == (
        "recommend"
    )
    assert (
        ledger["revisions"][0]["candidates"][0]["attributions"][0]["current_source_id"]
        == "A"
    )


def test_failed_refresh_keeps_active_acquisition_revision_and_records_failure(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cluster_acquisition_ledger.yml"
    active = {
        "cluster_id": "cluster-one",
        "cluster_revision_hash": "old",
        "candidate_input_hash": "old-input",
        "state": "active",
        "candidates": [
            {
                "external_source_id": "mapped-later",
                "writer_disposition": "recommend",
            }
        ],
    }
    failed = {
        "cluster_id": "cluster-one",
        "cluster_revision_hash": "new",
        "candidate_input_hash": "new-input",
        "state": "failure",
        "candidates": [],
    }
    _persist_cluster_acquisition_revisions(path, [active])
    payload = _persist_cluster_acquisition_revisions(path, [failed])

    assert {
        row["cluster_revision_hash"]: row["state"] for row in payload["revisions"]
    } == {
        "new": "failure",
        "old": "active",
    }
    retired = _persist_cluster_acquisition_revisions(
        path, [], mapped_external_source_ids={"mapped-later"}
    )
    old = next(
        row
        for row in retired["revisions"]
        if row["cluster_revision_hash"] == "old"
    )
    assert old["candidates"][0]["writer_disposition"] == "retired_mapped"


class ProviderEmptyResponse(RuntimeError):
    def __init__(self) -> None:
        super().__init__("provider returned empty response")
        self.raw_response = ""
        self.provider_completion = {"finish_reason": "stop", "content_characters": 0}


def test_empty_literature_response_retries_once_and_accounts_both_attempts(
    tmp_path: Path,
) -> None:
    class Reasoner:
        name = "local"
        model = "test"

        def __init__(self) -> None:
            self.calls = 0

        def propose_clusters(self, profiles, request, *, context=None):
            self.calls += 1
            if self.calls == 1:
                raise ProviderEmptyResponse()
            return {"clusters": []}

    reasoner = Reasoner()
    calls = _CheckpointedReasonerCalls(
        tmp_path,
        "empty-retry",
        reasoner,
        LiteratureMapRequest(tmp_path),
    )

    assert calls("cluster_proposal", "all", "propose_clusters", [], {}) == {
        "clusters": []
    }
    usage = read_yaml(calls.usage_path)
    attempts = sorted(usage["attempts"], key=lambda row: row["attempt"])
    assert reasoner.calls == 2
    assert usage["provider_call_count"] == 2
    assert [row["status"] for row in attempts] == ["failed", "completed"]
    assert attempts[0]["failure_class"] == "provider_empty_response"
    assert attempts[0]["raw_response"] == ""

    class ReplayReasoner:
        name = "local"
        model = "test"

        def propose_clusters(self, profiles, request, *, context=None):
            raise AssertionError("completed empty-response retry must replay locally")

    replay = _CheckpointedReasonerCalls(
        tmp_path,
        "empty-retry",
        ReplayReasoner(),
        LiteratureMapRequest(tmp_path),
    )
    assert replay("cluster_proposal", "all", "propose_clusters", [], {}) == {
        "clusters": []
    }
    assert replay.provider_calls == 0


def test_two_empty_literature_responses_are_terminal_after_two_attempts(
    tmp_path: Path,
) -> None:
    class Reasoner:
        name = "local"
        model = "test"

        def __init__(self) -> None:
            self.calls = 0

        def propose_clusters(self, profiles, request, *, context=None):
            self.calls += 1
            raise ProviderEmptyResponse()

    reasoner = Reasoner()
    calls = _CheckpointedReasonerCalls(
        tmp_path,
        "empty-twice",
        reasoner,
        LiteratureMapRequest(tmp_path),
    )
    with pytest.raises(Exception, match="terminal_failure"):
        calls("cluster_proposal", "all", "propose_clusters", [], {})

    failure = read_yaml(calls.root / "cluster_proposal/all.yml")
    assert reasoner.calls == 2
    assert failure["attempt_count"] == 2
    assert failure["failure_class"] == "provider_empty_response"
    assert failure["raw_response"] == ""


def test_cluster_scheduler_reserves_retry_capacity_and_accounts_deferrals(
    tmp_path: Path,
) -> None:
    class Reasoner:
        name = "local"
        model = "test"

        def __init__(self) -> None:
            self.calls = 0

        def propose_clusters(self, profiles, request, *, context=None):
            self.calls += 1
            if self.calls == 1:
                raise ProviderEmptyResponse()
            return {"clusters": []}

    request = LiteratureMapRequest(
        tmp_path,
        literature_policy=LiteratureMappingPolicy(max_synthesis_calls=32),
    )
    reasoner = Reasoner()
    calls = _CheckpointedReasonerCalls(
        tmp_path, "cluster-reserve", reasoner, request
    )
    calls.cumulative_provider_calls = 10
    runnable = [
        {"cluster_id": f"cluster-{index}", "revision_hash": f"revision-{index}"}
        for index in range(22)
    ]

    scheduled, deferred = _schedule_cluster_writers(calls, runnable)

    assert len(scheduled) == 15
    assert len(deferred) == 7
    assert all(
        _cluster_acquisition_revision(
            cluster,
            [
                {
                    "external_source_id": f"missing-{index}",
                    "status": "not_in_snapshot",
                }
            ],
            state="failure",
            default_disposition="deferred_budget",
        )["candidates"][0]["writer_disposition"]
        == "deferred_budget"
        for index, cluster in enumerate(deferred)
    )

    calls.cumulative_provider_calls = 25
    assert calls("cluster_proposal", "retry", "propose_clusters", [], {}) == {
        "clusters": []
    }
    assert calls.cumulative_provider_calls == 27


def test_stream_read_failure_is_transport() -> None:
    assert _synthesis_failure_class(RuntimeError("provider stream read failed")) == (
        "transport"
    )


def test_empty_retry_defers_when_call_ceiling_is_exhausted(tmp_path: Path) -> None:
    class Reasoner:
        name = "local"
        model = "test"

        def propose_clusters(self, profiles, request, *, context=None):
            raise ProviderEmptyResponse()

    calls = _CheckpointedReasonerCalls(
        tmp_path,
        "empty-budget",
        Reasoner(),
        LiteratureMapRequest(
            tmp_path,
            literature_policy=LiteratureMappingPolicy(max_synthesis_calls=1),
        ),
    )
    with pytest.raises(Exception, match="terminal_failure"):
        calls("cluster_proposal", "all", "propose_clusters", [], {})

    failure = read_yaml(calls.root / "cluster_proposal/all.yml")
    assert failure["empty_retry_status"] == "empty_retry_deferred_budget"
    assert calls.cumulative_provider_calls == 1
