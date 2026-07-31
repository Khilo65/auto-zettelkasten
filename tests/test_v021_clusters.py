from __future__ import annotations

from pathlib import Path

from auto_zettelkasten.readers import _parse_json_object
from typing import Any, Mapping, Sequence

import pytest

from auto_zettelkasten.files import read_yaml
from auto_zettelkasten.literature import (
    _CheckpointedReasonerCalls,
    _streamlined_cluster_markdown,
    _synthesis_stage_budget_group,
    _synthesis_stage_prompt_version,
    build_literature_report,
    validate_streamlined_cluster_synthesis,
)
from auto_zettelkasten.models import LiteratureMapRequest


def _profile(source_id: str) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "note_id": f"note-{source_id}",
        "note_path": f"02_source_memory/notes/{source_id}.md",
        "note_status": "analytical_atomic_note",
        "evidence_eligibility": "substantive_bounded",
        "title": f"Source {source_id}",
        "thesis": f"Thesis for {source_id}.",
        "methods": ["comparative analysis"],
        "study_family_id": source_id,
        "evidence_anchors": [
            {
                "evidence_anchor_id": f"anchor-{source_id}",
                "source_id": source_id,
                "claim": f"Finding for {source_id}.",
                "locator": "p. 10",
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


def _response(
    cluster: Mapping[str, Any],
    profiles: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    source_ids = [str(row["source_id"]) for row in profiles]
    return {
        "cluster_contract": "streamlined-full-note-v1",
        "cluster_id": str(cluster["cluster_id"]),
        "status": "accepted",
        "title": "Shared family",
        "organizing_mode": "question",
        "organizing_problem": "How do the studies address the problem?",
        "bottom_line": "The sources make distinct, bounded contributions.",
        "retained_member_ids": source_ids,
        "dropped_members": [],
        "material_exclusions": [],
        "important_cited_works_not_yet_mapped": [],
        "differences": [],
        "limits": [],
        "related_clusters": [],
        "lines_of_inquiry": [
            {
                "title": "Main line",
                "synthesis": "The sources address related parts of the problem.",
                "study_findings": [
                    {
                        "source_id": source_id,
                        "finding": f"{source_id} makes a specific contribution.",
                        "method_scope": "Comparative analysis.",
                        "relation_to_line": "supports",
                        "evidence": [
                            {
                                "source_id": source_id,
                                "evidence_anchor_id": f"anchor-{source_id}",
                                "locator": "p. 10",
                            }
                        ],
                    }
                    for source_id in source_ids
                ],
            }
        ],
    }


def test_shared_family_plan_bypasses_cluster_planner_and_supplies_receipt() -> None:
    profiles = [_profile("A"), _profile("B")]
    notes = [
        {
            "source_id": source_id,
            "title": f"Source {source_id}",
            "source_scope": "full_document",
            "body": f"# Source {source_id}\n\n## Thesis\n\nFull note {source_id}.",
        }
        for source_id in ("A", "B")
    ]
    seen_context: dict[str, Any] = {}

    class Reasoner:
        name = "local"
        model = "test"

        def plan_clusters(self, profiles, request, *, context=None):
            raise AssertionError("shared plan must bypass plan_clusters")

        def synthesize_cluster(self, projected, request, *, context=None):
            seen_context.update(dict(context or {}))
            return _response(context["cluster"], projected)

    report = build_literature_report(
        profiles,
        reasoner=Reasoner(),
        request=LiteratureMapRequest(Path(".")),
        source_notes=notes,
        shared_literature_plan={
            "literature_families": [
                {
                    "family_id": "shared-family",
                    "label": "Shared family",
                    "organizing_problem": "How do the studies address the problem?",
                    "source_ids": ["A", "B"],
                    "proposed_roles": {
                        "A": "core",
                        "B": "boundary",
                    },
                    "candidate_cluster": True,
                }
            ],
            "discovery_jobs": [],
            "neighboring_families": [],
        },
        literature_positions=[
            {
                "literature_position_id": "position-A-B",
                "current_source_id": "A",
                "matched_source_id": "B",
                "engagement": "A treats B as a boundary condition.",
                "match_status": "matched",
            }
        ],
        missing_sources=[
            {
                "external_source_id": "missing-1",
                "raw_citation": "Absent 1999",
                "discussed_by_source_ids": ["A"],
                "importance": "Defines the central mechanism.",
                "match_status": "not_in_snapshot",
            },
            {
                "external_source_id": "ambiguous",
                "raw_citation": "Ambiguous 2001",
                "discussed_by_source_ids": ["A"],
                "importance": "Ambiguous record.",
                "match_status": "ambiguous",
            },
        ],
    )

    cluster = report["cluster_registry"]["clusters"][0]
    synthesis = report["cluster_syntheses"][cluster["cluster_id"]]
    receipt = seen_context["candidate_input_receipt"]

    assert cluster["candidate_roles"] == {"A": "core", "B": "boundary"}
    assert receipt["candidate_source_ids"] == ["A", "B"]
    assert {row["source_id"] for row in receipt["sources"]} == {"A", "B"}
    assert seen_context["literature_positions"][0]["matched_source_id"] == "B"
    assert [
        row["external_source_id"]
        for row in seen_context["important_unmapped_literature"]
    ] == ["missing-1"]
    assert synthesis["candidate_input_receipt"] == receipt


def test_one_malformed_shared_plan_cluster_is_parked_without_partial_map() -> None:
    profiles = [_profile("A"), _profile("B")]

    class Reasoner:
        name = "local"
        model = "test"

        def synthesize_cluster(self, projected, request, *, context=None):
            return {}

    report = build_literature_report(
        profiles,
        reasoner=Reasoner(),
        request=LiteratureMapRequest(Path(".")),
        source_notes=[
            {
                "source_id": source_id,
                "title": f"Source {source_id}",
                "source_scope": "full_document",
                "body": f"# Source {source_id}\n\nFull note.",
            }
            for source_id in ("A", "B")
        ],
        shared_literature_plan={
            "literature_families": [
                {
                    "family_id": "shared-family",
                    "label": "Shared family",
                    "organizing_problem": "A bounded problem",
                    "source_ids": ["A", "B"],
                    "proposed_roles": {"A": "core", "B": "supporting"},
                    "candidate_cluster": True,
                }
            ],
            "discovery_jobs": [],
            "neighboring_families": [],
        },
    )

    assert report["packet"]["status"] == "complete"
    assert report["cluster_registry"]["clusters"] == []
    assert len(report["packet"]["parked_cluster_ids"]) == 1


def test_malformed_cluster_with_no_publishable_prior_stays_parked() -> None:
    profiles = [_profile("A"), _profile("B")]
    shared_plan = {
        "literature_families": [
            {
                "family_id": "shared-family",
                "label": "Shared family",
                "organizing_problem": "A bounded problem",
                "source_ids": ["A", "B"],
                "proposed_roles": {"A": "core", "B": "supporting"},
                "candidate_cluster": True,
            }
        ],
        "discovery_jobs": [],
        "neighboring_families": [],
    }

    class ValidReasoner:
        name = "local"
        model = "test"

        def synthesize_cluster(self, projected, request, *, context=None):
            return _response(context["cluster"], projected)

    first = build_literature_report(
        profiles,
        reasoner=ValidReasoner(),
        request=LiteratureMapRequest(Path(".")),
        source_notes=[
            {
                "source_id": source_id,
                "title": f"Source {source_id}",
                "source_scope": "full_document",
                "body": f"# Source {source_id}\n\nFull note.",
            }
            for source_id in ("A", "B")
        ],
        shared_literature_plan=shared_plan,
    )
    cluster_id = first["cluster_registry"]["clusters"][0]["cluster_id"]
    previous = dict(first["cluster_registry"])
    previous["cluster_syntheses"] = {
        cluster_id: {"status": "partial", "quality_status": "failed"}
    }

    class MalformedReasoner:
        name = "local"
        model = "test"

        def synthesize_cluster(self, projected, request, *, context=None):
            return {}

    second = build_literature_report(
        profiles,
        reasoner=MalformedReasoner(),
        request=LiteratureMapRequest(Path(".")),
        source_notes=[
            {
                "source_id": source_id,
                "title": f"Source {source_id}",
                "source_scope": "full_document",
                "body": f"# Source {source_id}\n\nFull note.",
            }
            for source_id in ("A", "B")
        ],
        previous_registry=previous,
        shared_literature_plan=shared_plan,
    )

    assert second["cluster_registry"]["clusters"] == []
    assert second["packet"]["parked_cluster_ids"] == [cluster_id]


def test_cluster_json_recovers_call_punctuation_after_string_value() -> None:
    payload = _parse_json_object(
        '{"cluster_id":"cluster-a","material_exclusions":"Not used.");}',
        label="cluster synthesis response",
    )

    assert payload == {
        "cluster_id": "cluster-a",
        "material_exclusions": "Not used.",
    }


def test_shared_family_admits_only_bounded_partial_with_partial_role() -> None:
    partial = _profile("P")
    partial.update(
        {
            "note_status": "partial_document_atomic_note",
            "analytical": False,
        }
    )
    partial["evidence_anchors"][0]["support_envelope"]["coverage"] = "limited_text"
    metadata = _profile("M")
    metadata.update(
        {
            "note_status": "metadata_only_source_note",
            "analytical": False,
            "evidence_eligibility": "context_only",
        }
    )
    profiles = [_profile("A"), _profile("B"), partial, metadata]
    notes = [
        {
            "source_id": source_id,
            "title": f"Source {source_id}",
            "source_scope": (
                "partial_document" if source_id == "P" else "full_document"
            ),
            "body": f"# Source {source_id}\n\n## Thesis\n\nFull note {source_id}.",
        }
            for source_id in ("A", "B", "P", "M")
    ]

    class Reasoner:
        name = "local"
        model = "test"

        def synthesize_cluster(self, projected, request, *, context=None):
            return _response(context["cluster"], projected)

    report = build_literature_report(
        profiles,
        reasoner=Reasoner(),
        request=LiteratureMapRequest(Path(".")),
        source_notes=notes,
        shared_literature_plan={
            "literature_families": [
                {
                    "family_id": "shared-family",
                    "label": "Shared family",
                    "organizing_problem": "How do the studies address the problem?",
                    "source_ids": ["A", "B", "P", "M"],
                    "proposed_roles": {
                        "A": "core",
                        "B": "core",
                        "P": "partial",
                        "M": "boundary",
                    },
                    "candidate_cluster": True,
                }
            ],
            "discovery_jobs": [],
            "neighboring_families": [],
        },
    )

    cluster = report["cluster_registry"]["clusters"][0]
    synthesis = report["cluster_syntheses"][cluster["cluster_id"]]
    assert cluster["candidate_roles"]["P"] == "partial"
    assert "P" in cluster["source_ids"]
    assert "M" not in cluster["source_ids"]
    assert synthesis["candidate_input_receipt"]["sources"][-1][
        "source_scope"
    ] == "partial_document"


def test_streamlined_validation_keeps_optional_fields_and_no_plain_fallback() -> None:
    profiles = [_profile("A"), _profile("B"), _profile("C")]
    cluster = {
        "cluster_id": "cluster-one",
        "source_ids": ["A", "B", "C"],
    }
    response = _response(cluster, profiles[:2])
    response["material_exclusions"] = [
        {
            "source_id": "C",
            "boundary": "C addresses onset rather than recurrence.",
        },
        {
            "source_id": "UNKNOWN",
            "boundary": "Unknown source.",
        },
    ]
    response["important_cited_works_not_yet_mapped"] = [
        {
            "external_source_id": "missing-1",
            "cited_work": "Absent 1999",
            "invoked_by_source_ids": ["A"],
            "characterization": "A identifies it as the origin of the mechanism.",
            "why_it_matters": "It defines the cluster boundary.",
            "status": "not_in_snapshot",
        }
    ]

    validated = validate_streamlined_cluster_synthesis(
        response,
        cluster,
        profiles,
        candidate_input_receipt={
            "cluster_id": "cluster-one",
            "candidate_source_ids": ["A", "B", "C"],
            "sources": [],
        },
    )

    assert validated["source_contributions"][0]["plain_english_meaning"] == ""
    assert validated["evidence_threads"][0]["plain_english_meaning"] == ""
    assert validated["material_exclusions"] == [
        {
            "source_id": "C",
            "boundary": "C addresses onset rather than recurrence.",
        }
    ]
    assert validated["important_cited_works_not_yet_mapped"][0][
        "invoked_by_source_ids"
    ] == ["A"]
    assert validated["candidate_input_receipt"]["candidate_source_ids"] == [
        "A",
        "B",
        "C",
    ]


def test_streamlined_markdown_groups_repeated_findings_under_one_source_block() -> None:
    profiles = [_profile("A"), _profile("B")]
    profile_by_source = {row["source_id"]: row for row in profiles}
    cluster = {
        "cluster_id": "cluster-one",
        "label": "Shared family",
        "source_ids": ["A", "B"],
    }
    synthesis = _response(cluster, profiles)
    synthesis["lines_of_inquiry"].append(
        {
            "title": "Boundary line",
            "synthesis": "A also establishes an important boundary.",
            "study_findings": [
                {
                    "source_id": "A",
                    "finding": "A limits the result to a narrower population.",
                    "method_scope": "Comparative analysis.",
                    "relation_to_line": "qualifies",
                    "evidence": [
                        {
                            "source_id": "A",
                            "evidence_anchor_id": "anchor-A",
                            "locator": "p. 11",
                        }
                    ],
                }
            ],
        }
    )
    synthesis["material_exclusions"] = [
        {
            "source_id": "B",
            "boundary": "B measures a neighboring outcome.",
        }
    ]
    synthesis["important_cited_works_not_yet_mapped"] = [
        {
            "cited_work": "Absent 1999",
            "invoked_by_source_ids": ["A"],
            "characterization": "A treats this as foundational.",
            "why_it_matters": "It explains the mechanism's origin.",
            "status": "not_in_snapshot",
        }
    ]

    markdown = _streamlined_cluster_markdown(
        cluster,
        synthesis,
        profile_by_source=profile_by_source,
        cluster_by_id={"cluster-one": cluster},
    )

    assert markdown.count("#### [[A|Source A]]") == 1
    assert markdown.count("*Method: Comparative analysis.*") == 2
    assert "See the primary [[A|Source A]] discussion under **Main line**" in markdown
    assert "## Important cited works not yet mapped" in markdown
    assert "## Material exclusions" in markdown
    assert "In plain English:" not in markdown


def test_literature_family_stage_has_explicit_checkpoint_mappings() -> None:
    assert _synthesis_stage_prompt_version("literature_family_plan") == "6"
    assert (
        _synthesis_stage_budget_group("literature_family_plan")
        == "literature_family_plan"
    )


def test_family_plan_failure_checkpoint_preserves_raw_text_and_completion(
    tmp_path: Path,
) -> None:
    class Failure(Exception):
        pass

    class Reasoner:
        name = "local"
        model = "test"

        def literature_family_plan_fits(
            self, profiles, request, *, context=None
        ):
            return True

        def plan_literature_families(
            self, profiles, request, *, context=None
        ):
            exc = Failure("malformed family response")
            exc.raw_response = "```json\n{\"literature_families\": []\n```"
            exc.provider_completion = {
                "finish_reason": "length",
                "max_output_tokens": 64_000,
            }
            raise exc

    calls = _CheckpointedReasonerCalls(
        tmp_path,
        "run-one",
        Reasoner(),
        LiteratureMapRequest(tmp_path),
    )
    with pytest.raises(Failure):
        calls(
            "literature_family_plan",
            "global",
            "plan_literature_families",
            [],
            {},
        )

    checkpoint = read_yaml(
        tmp_path
        / "11_state"
        / "runs"
        / "run-one"
        / "literature"
        / "synthesis"
        / "literature_family_plan"
        / "global.yml"
    )
    assert checkpoint["raw_response"].startswith("```json")
    assert checkpoint["provider_completion"]["finish_reason"] == "length"
