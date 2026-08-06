from __future__ import annotations

from pathlib import Path

from auto_zettelkasten.cli import build_parser
from auto_zettelkasten.files import write_yaml
from auto_zettelkasten.indexes import lean_discovery_projection
from auto_zettelkasten.literature import _validate_literature_family_plan_response
from auto_zettelkasten.models import LiteratureMapRequest
from auto_zettelkasten.pipeline import (
    _merge_literature_family_plans,
    _plan_literature_families,
    _validate_literature_family_plan,
)


def test_lean_discovery_projection_preserves_complete_profile_text() -> None:
    thesis = "Complete thesis " + ("with consequential detail " * 40)
    method = "Complete method " + ("with design detail " * 25)
    rows = lean_discovery_projection(
        [
            {
                "source_id": "A",
                "context": {
                    "thesis": thesis,
                    "method_or_knowledge_basis": method,
                },
                "evidence_eligibility": "substantive_bounded",
            }
        ],
        {
            "sources": [
                {
                    "source_id": "A",
                    "zotero_key": "ZA",
                    "title": "Source A",
                    "author": "Author",
                    "year": "2020",
                    "thesis": "truncated catalogue thesis",
                    "method": "truncated catalogue method",
                    "collection_keys": ["C1"],
                }
            ]
        },
    )

    assert rows[0]["thesis"] == thesis.strip()
    assert rows[0]["method"] == method.strip()


def test_shared_plan_adds_missing_explicit_collection_comparison() -> None:
    result = _validate_literature_family_plan(
        {
            "literature_families": [
                {
                    "family_id": "family",
                    "label": "Family",
                    "organizing_problem": "A bounded problem",
                    "source_ids": ["A", "B"],
                    "proposed_roles": {"A": "core", "B": "supporting"},
                }
            ],
            "discovery_jobs": [
                {
                    "job_id": "within-family",
                    "family": "family",
                    "left_source_ids": ["A"],
                    "right_source_ids": ["B"],
                    "candidate_quota": 12,
                }
            ],
            "neighboring_families": [],
        },
        lean_rows=[
            {"source_id": "A", "collection_keys": ["C1"]},
            {"source_id": "B", "collection_keys": ["C2"]},
        ],
        requested_collection_keys=["C1", "C2"],
    )

    requested = [
        row
        for row in result["discovery_jobs"]
        if row["requested_collection_pair"] == ["C1", "C2"]
    ]
    assert len(requested) == 1
    assert requested[0]["left_source_ids"] == ["A"]
    assert requested[0]["right_source_ids"] == ["B"]


def test_family_plan_normalizes_boolean_requested_pair_without_retry() -> None:
    result = _validate_literature_family_plan_response(
        {
            "literature_families": [
                {
                    "family_id": "family",
                    "label": "Family",
                    "organizing_problem": "A bounded problem",
                    "source_ids": ["A", "A", "B"],
                }
            ],
            "discovery_jobs": [
                {
                    "job_id": "job",
                    "left_source_ids": ["A"],
                    "right_source_ids": ["B"],
                    "requested_collection_pair": True,
                }
            ],
            "neighboring_families": [],
        }
    )

    assert result["literature_families"][0]["source_ids"] == ["A", "B"]
    assert result["discovery_jobs"][0]["requested_collection_pair"] == []


def test_build_map_cli_accepts_repeatable_collection_comparisons() -> None:
    args = build_parser().parse_args(
        [
            "build-map",
            "--workspace",
            "/tmp/workspace",
            "--compare-collection",
            "C1",
            "--compare-collection",
            "C2",
        ]
    )

    assert args.comparison_collection_keys == ["C1", "C2"]


def test_incremental_family_plan_preserves_unaffected_families(
    tmp_path: Path,
) -> None:
    class Reasoner:
        name = "local"
        model = "test"

        def literature_family_plan_fits(self, profiles, request, *, context=None):
            return True

        def plan_literature_families(self, profiles, request, *, context=None):
            raise AssertionError("checkpoint wrapper supplies the response")

    class Calls:
        def __init__(self, responses):
            self.responses = iter(responses)

        def __call__(self, *args):
            return next(self.responses)

    catalogue_path = tmp_path / "02_source_memory" / "indexes" / "catalogue.yml"
    write_yaml(
        catalogue_path,
        {
            "sources": [
                {
                    "source_id": source_id,
                    "zotero_key": source_id,
                    "title": f"Source {source_id}",
                    "author": "Author",
                    "year": "2020",
                    "collection_keys": ["C1"],
                }
                for source_id in ("A", "B", "C", "D")
            ],
            "collections": [
                {
                    "key": "C1",
                    "name": "Collection",
                    "direct_source_ids": ["A", "B", "C", "D"],
                }
            ],
        },
    )
    profiles = [
        {
            "source_id": source_id,
            "context": {
                "thesis": f"Thesis {source_id}",
                "method_or_knowledge_basis": "Comparative analysis",
            },
        }
        for source_id in ("A", "B", "C", "D")
    ]
    initial = {
        "literature_families": [
            {
                "family_id": "family-ab",
                "label": "AB",
                "organizing_problem": "Problem AB",
                "source_ids": ["A", "B"],
                "proposed_roles": {"A": "core", "B": "supporting"},
            },
            {
                "family_id": "family-cd",
                "label": "CD",
                "organizing_problem": "Problem CD",
                "source_ids": ["C", "D"],
                "proposed_roles": {"C": "core", "D": "supporting"},
            },
        ],
        "discovery_jobs": [
            {
                "job_id": "discover-ab",
                "family": "family-ab",
                "left_source_ids": ["A"],
                "right_source_ids": ["B"],
            },
            {
                "job_id": "discover-cd",
                "family": "family-cd",
                "left_source_ids": ["C"],
                "right_source_ids": ["D"],
            },
        ],
        "neighboring_families": [],
    }
    request = LiteratureMapRequest(tmp_path)
    first = _plan_literature_families(
        tmp_path,
        profiles=profiles,
        catalogue={"catalogue_path": str(catalogue_path)},
        reasoner=Reasoner(),
        reasoner_calls=Calls([initial]),
        request=request,
    )
    changed_profiles = [dict(row) for row in profiles]
    changed_profiles[0] = {
        **changed_profiles[0],
        "context": {
            **changed_profiles[0]["context"],
            "thesis": "Changed thesis A",
        },
    }
    patch = {
        "literature_families": [
            {
                "family_id": "family-ab",
                "label": "AB revised",
                "organizing_problem": "Problem AB",
                "source_ids": ["A", "B"],
                "proposed_roles": {"A": "core", "B": "supporting"},
            }
        ],
        "discovery_jobs": [
            {
                "job_id": "discover-ab",
                "family": "family-ab",
                "left_source_ids": ["A"],
                "right_source_ids": ["B"],
            }
        ],
        "neighboring_families": [],
    }
    second = _plan_literature_families(
        tmp_path,
        profiles=changed_profiles,
        catalogue={"catalogue_path": str(catalogue_path)},
        reasoner=Reasoner(),
        reasoner_calls=Calls([patch]),
        request=request,
    )

    assert first is not None and second is not None
    assert second["planning_path"] == "incremental_patch"
    assert second["incremental_source_ids"] == ["A"]
    assert {
        row["family_id"]: row["label"]
        for row in second["literature_families"]
    } == {"family-ab": "AB revised", "family-cd": "CD"}


def test_initial_family_plan_adds_coverage_completion_family(
    tmp_path: Path,
) -> None:
    class Reasoner:
        name = "local"
        model = "test"

        def literature_family_plan_fits(self, profiles, request, *, context=None):
            return True

        def plan_literature_families(self, profiles, request, *, context=None):
            raise AssertionError("checkpoint wrapper supplies responses")

    class Calls:
        def __init__(self) -> None:
            self.contexts = []

        def __call__(self, _stage, _key, _method, _profiles, context):
            self.contexts.append(context)
            if context["planning_mode"] == "coverage_completion":
                return {
                    "literature_families": [
                        {
                            "family_id": "ceasefire-peacekeeping",
                            "label": "Ceasefire design and peacekeeping",
                            "organizing_problem": "How ceasefire design and peacekeeping shape durability",
                            "source_ids": ["C", "D"],
                            "proposed_roles": {"C": "core", "D": "supporting"},
                        }
                    ],
                    "discovery_jobs": [
                        {
                            "job_id": "discover-ceasefire",
                            "family": "ceasefire-peacekeeping",
                            "left_source_ids": ["C"],
                            "right_source_ids": ["D"],
                        }
                    ],
                    "neighboring_families": [],
                }
            return {
                "literature_families": [
                    {
                        "family_id": "mediation",
                        "label": "Mediation",
                        "organizing_problem": "How mediation operates",
                        "source_ids": ["A", "B"],
                        "proposed_roles": {"A": "core", "B": "supporting"},
                    },
                    {
                        "family_id": "singleton-c",
                        "label": "Singleton C",
                        "organizing_problem": "A provisional singleton",
                        "source_ids": ["C"],
                    },
                ],
                "discovery_jobs": [
                    {
                        "job_id": "discover-mediation",
                        "family": "mediation",
                        "left_source_ids": ["A"],
                        "right_source_ids": ["B"],
                    }
                ],
                "neighboring_families": [],
                "source_dispositions": [
                    {
                        "source_id": "C",
                        "disposition": "assigned",
                        "family_ids": ["singleton-c"],
                    }
                ],
            }

    catalogue_path = tmp_path / "02_source_memory" / "indexes" / "catalogue.yml"
    write_yaml(
        catalogue_path,
        {
            "sources": [
                {
                    "source_id": source_id,
                    "zotero_key": source_id,
                    "title": f"Source {source_id}",
                    "author": "Author",
                    "year": "2020",
                    "collection_keys": ["C1"],
                }
                for source_id in "ABCD"
            ],
            "collections": [
                {"key": "C1", "name": "Collection", "direct_source_ids": list("ABCD")}
            ],
        },
    )
    profiles = [
        {
            "source_id": source_id,
            "context": {
                "thesis": f"Thesis {source_id}",
                "method_or_knowledge_basis": "Comparative analysis",
            },
        }
        for source_id in "ABCD"
    ]
    calls = Calls()

    result = _plan_literature_families(
        tmp_path,
        profiles=profiles,
        catalogue={"catalogue_path": str(catalogue_path)},
        reasoner=Reasoner(),
        reasoner_calls=calls,
        request=LiteratureMapRequest(tmp_path),
    )

    assert [row["family_id"] for row in result["literature_families"]] == [
        "coverage-packet-d686b243a7679f1e:ceasefire-peacekeeping",
        "mediation",
    ]
    assert [context["planning_mode"] for context in calls.contexts] == [
        "initial_global",
        "coverage_completion",
    ]
    assert calls.contexts[1]["unassigned_source_ids"] == ["C", "D"]


def test_family_plan_discards_placeholder_requested_pair() -> None:
    result = _validate_literature_family_plan(
        {
            "literature_families": [
                {
                    "family_id": "family",
                    "label": "Family",
                    "organizing_problem": "A bounded problem",
                    "source_ids": ["A", "B"],
                }
            ],
            "discovery_jobs": [
                {
                    "job_id": "placeholder",
                    "family": "family",
                    "left_source_ids": ["A"],
                    "right_source_ids": ["B"],
                    "requested_collection_pair": [
                        "left_collection_key",
                        "right_collection_key",
                    ],
                }
            ],
            "neighboring_families": [],
        },
        lean_rows=[
            {"source_id": "A", "collection_keys": ["C1"]},
            {"source_id": "B", "collection_keys": ["C2"]},
        ],
        requested_collection_keys=["C1", "C2"],
    )

    jobs = {row["job_id"]: row for row in result["discovery_jobs"]}
    assert jobs["placeholder"]["requested_collection_pair"] == []
    assert any(
        row["requested_collection_pair"] == ["C1", "C2"]
        for row in result["discovery_jobs"]
    )


def test_singleton_family_assignment_remains_pending_for_coverage() -> None:
    response = {
        "literature_families": [
            {
                "family_id": "singleton",
                "label": "Singleton",
                "source_ids": ["A"],
            },
            {
                "family_id": "admitted",
                "label": "Admitted",
                "source_ids": ["B", "C"],
            },
        ],
        "discovery_jobs": [],
        "neighboring_families": [],
        "source_dispositions": [
            {
                "source_id": "A",
                "disposition": "assigned",
                "family_ids": ["singleton"],
            }
        ],
    }
    result = _validate_literature_family_plan(
        response,
        lean_rows=[
            {"source_id": "A", "collection_keys": []},
            {"source_id": "B", "collection_keys": []},
            {"source_id": "C", "collection_keys": []},
        ],
        requested_collection_keys=[],
        allow_empty=True,
    )

    dispositions = {
        row["source_id"]: row for row in result["source_dispositions"]
    }
    assert dispositions["A"] == {
        "source_id": "A",
        "disposition": "pending",
        "family_ids": [],
        "reason": "assigned_singleton_family_pending_completion",
    }
    assert result["unaccounted_source_ids"] == ["A"]

    incremental = _validate_literature_family_plan(
        response,
        lean_rows=[
            {"source_id": "A", "collection_keys": []},
            {"source_id": "B", "collection_keys": []},
            {"source_id": "C", "collection_keys": []},
        ],
        requested_collection_keys=[],
        allow_empty=True,
        settle_singletons=True,
    )
    incremental_dispositions = {
        row["source_id"]: row for row in incremental["source_dispositions"]
    }
    assert incremental_dispositions["A"]["disposition"] == "currently_unclustered"
    assert incremental["unaccounted_source_ids"] == []


def test_unknown_family_assignment_remains_unaccounted() -> None:
    result = _validate_literature_family_plan(
        {
            "literature_families": [
                {"family_id": "admitted", "source_ids": ["B", "C"]}
            ],
            "discovery_jobs": [],
            "neighboring_families": [],
            "source_dispositions": [
                {
                    "source_id": "A",
                    "disposition": "assigned",
                    "family_ids": ["hallucinated-family"],
                }
            ],
        },
        lean_rows=[
            {"source_id": "A", "collection_keys": []},
            {"source_id": "B", "collection_keys": []},
            {"source_id": "C", "collection_keys": []},
        ],
        requested_collection_keys=[],
        allow_empty=True,
    )

    dispositions = {
        row["source_id"]: row for row in result["source_dispositions"]
    }
    assert dispositions["A"]["disposition"] == "pending"
    assert dispositions["A"]["reason"] == "planning_packet_did_not_account_for_source"


def test_invalid_coverage_does_not_settle_initial_singleton() -> None:
    rows = [
        {"source_id": "A", "collection_keys": []},
        {"source_id": "B", "collection_keys": []},
        {"source_id": "C", "collection_keys": []},
    ]
    initial = _validate_literature_family_plan(
        {
            "literature_families": [
                {"family_id": "singleton", "source_ids": ["A"]},
                {"family_id": "admitted", "source_ids": ["B", "C"]},
            ],
            "discovery_jobs": [],
            "source_dispositions": [
                {
                    "source_id": "A",
                    "disposition": "assigned",
                    "family_ids": ["singleton"],
                }
            ],
        },
        lean_rows=rows,
        requested_collection_keys=[],
        allow_empty=True,
    )
    invalid_completion = _validate_literature_family_plan(
        {
            "literature_families": [],
            "discovery_jobs": [],
            "source_dispositions": [
                {
                    "source_id": "A",
                    "disposition": "assigned",
                    "family_ids": ["hallucinated-family"],
                }
            ],
        },
        lean_rows=rows,
        requested_collection_keys=[],
        allow_empty=True,
        settle_singletons=True,
    )

    merged = _merge_literature_family_plans(initial, invalid_completion)
    dispositions = {
        row["source_id"]: row for row in merged["source_dispositions"]
    }
    assert dispositions["A"]["disposition"] == "pending"
    assert dispositions["A"]["reason"] == "planning_packet_did_not_account_for_source"


def test_narrow_family_job_does_not_replace_full_requested_comparison() -> None:
    result = _validate_literature_family_plan(
        {
            "literature_families": [
                {
                    "family_id": "narrow",
                    "label": "Narrow family",
                    "organizing_problem": "One part of the comparison",
                    "source_ids": ["A", "B"],
                }
            ],
            "discovery_jobs": [
                {
                    "job_id": "narrow-job",
                    "family": "narrow",
                    "left_source_ids": ["A"],
                    "right_source_ids": ["B"],
                    "requested_collection_pair": ["C1", "C2"],
                }
            ],
            "neighboring_families": [],
        },
        lean_rows=[
            {"source_id": "A", "collection_keys": ["C1"]},
            {"source_id": "B", "collection_keys": ["C2"]},
            {"source_id": "C", "collection_keys": ["C1"]},
            {"source_id": "D", "collection_keys": ["C2"]},
        ],
        requested_collection_keys=["C1", "C2"],
    )

    requested_jobs = [
        row
        for row in result["discovery_jobs"]
        if row["requested_collection_pair"] == ["C1", "C2"]
    ]
    assert len(requested_jobs) == 2
    assert any(
        row["left_source_ids"] == ["A", "C"]
        and row["right_source_ids"] == ["B", "D"]
        for row in requested_jobs
    )
