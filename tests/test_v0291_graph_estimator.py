from pathlib import Path

from auto_zettelkasten.api import estimate_cost
from auto_zettelkasten.files import write_yaml


def _frozen_profiles(root: Path, count: int) -> None:
    for index in range(count):
        write_yaml(
            root / "02_source_memory" / "profiles" / f"note-{index}.yml",
            {"profile": {"note_id": f"note-{index}"}},
        )
        note = root / "02_source_memory" / "notes" / f"note-{index}.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(f"# Source {index}\n", encoding="utf-8")


def _source_set(root: Path, *, inventory: int, validated: int) -> None:
    write_yaml(
        root
        / "02_source_memory"
        / "indexes"
        / "source_sets"
        / "source-set-auto-zettelkasten-workspace.yml",
        {"inventory_count": inventory, "validated_note_count": validated},
    )


def test_graph_estimate_does_not_treat_zero_validated_count_as_empty(
    tmp_path: Path,
) -> None:
    _frozen_profiles(tmp_path, 425)
    _source_set(tmp_path, inventory=425, validated=0)

    result = estimate_cost(tmp_path, graph_only=True)

    assert result["new_source_jobs"] == 0
    assert result["graph_calls"]["expected"] == 122
    assert result["graph_calls"]["high"] >= 113
    assert result["graph_cost_usd"]["high_usd"] >= 3.55
    assert result["graph_call_components"]["breadth_completion"] == {
        "low": 1,
        "expected": 1,
        "high": 1,
    }
    assert result["graph_estimate_provenance"]["planning_profile_count"] == 425
    assert (
        result["graph_estimate_provenance"]["graph_neighborhood_profile_count"] == 425
    )


def test_incremental_estimate_prices_delta_and_bounded_neighborhood(
    tmp_path: Path,
) -> None:
    _frozen_profiles(tmp_path, 450)
    _source_set(tmp_path, inventory=425, validated=425)
    write_yaml(
        tmp_path / "02_source_memory" / "indexes" / "literature_family_plan.yml",
        {"lean_source_hashes": {f"source-{index}": "hash" for index in range(425)}},
    )
    write_yaml(
        tmp_path / "evaluation" / "v029-incremental-activation.yml",
        {"activated": True, "source_count": 25},
    )

    result = estimate_cost(tmp_path, graph_only=True)
    provenance = result["graph_estimate_provenance"]

    assert provenance["incremental_profile_count"] == 25
    assert provenance["planning_profile_count"] == 25
    assert provenance["graph_neighborhood_profile_count"] == 125
    assert provenance["graph_neighborhood_profile_counts"] == {
        "low": 75,
        "expected": 125,
        "high": 450,
    }
    assert result["graph_calls"]["expected"] == 38
    assert result["source_cost_usd"]["expected_usd"] == 0
    assert result["provider_calls"] == 0


def test_graph_estimate_prices_known_cluster_completion_work(
    tmp_path: Path,
) -> None:
    _frozen_profiles(tmp_path, 6)
    _source_set(tmp_path, inventory=6, validated=6)
    source_ids = [f"source-{index}" for index in range(6)]
    note_ids = [f"note-{index}" for index in range(6)]
    for index, note_id in enumerate(note_ids):
        write_yaml(
            tmp_path / "02_source_memory" / "profiles" / f"{note_id}.yml",
            {
                "profile": {
                    "note_id": note_id,
                    "source_id": source_ids[index],
                    "context": {"note_path": f"02_source_memory/notes/{note_id}.md"},
                }
            },
        )
    write_yaml(
        tmp_path / "03_literature_synthesis" / "cluster_registry.yml",
        {
            "clusters": [],
            "pending_revisions": [
                {
                    "cluster": {
                        "cluster_id": "cluster-oversized",
                        "source_ids": source_ids,
                        "note_ids": note_ids,
                    }
                }
            ],
        },
    )
    checkpoint_root = (
        tmp_path
        / "11_state"
        / "runs"
        / "prior-run"
        / "literature"
        / "synthesis"
        / "cluster_synthesis"
    )
    write_yaml(
        checkpoint_root / "cluster-oversized.yml",
        {
            "key": "cluster-oversized",
            "status": "failed",
            "failure_class": "terminal",
            "deterministic_preflight": True,
            "error": {
                "message": (
                    "literature_provider_context_budget_exceeded:"
                    "cluster_synthesis:cluster-oversized"
                )
            },
            "updated_at": "2026-08-10T00:00:00+00:00",
        },
    )
    write_yaml(
        checkpoint_root / "cluster-empty.yml",
        {
            "key": "cluster-empty",
            "status": "failed",
            "failure_class": "provider_empty_response",
            "attempt_count": 2,
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "provider_completion": {
                "max_output_tokens": 128_000,
                "usage": {"prompt_tokens": 12_345},
            },
            "updated_at": "2026-08-10T00:00:00+00:00",
        },
    )

    result = estimate_cost(tmp_path, graph_only=True)
    components = result["graph_call_components"]
    provenance = result["graph_estimate_provenance"]

    assert components["cluster_partition_planning"] == {
        "low": 1,
        "expected": 1,
        "high": 1,
    }
    assert components["cluster_child_synthesis"] == {
        "low": 2,
        "expected": 2,
        "high": 3,
    }
    assert components["empty_response_recovery"] == {
        "low": 1,
        "expected": 1,
        "high": 1,
    }
    assert provenance["oversized_cluster_parent_count"] == 1
    assert provenance["cluster_child_writer_counts"] == {
        "low": 2,
        "expected": 2,
        "high": 3,
    }
    assert provenance["eligible_empty_recovery_count"] == 1
    assert provenance["eligible_empty_recovery_input_tokens"] == 12_345
    assert provenance["cluster_writer_input_budget_characters"] == 2_009_856
    assert (
        result["graph_stage_estimates"]["empty_response_recovery"]["expected"][
            "input_tokens"
        ]
        == 12_345
    )
