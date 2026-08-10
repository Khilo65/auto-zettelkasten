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
    assert result["graph_calls"]["expected"] == 121
    assert result["graph_calls"]["high"] >= 107
    assert result["graph_cost_usd"]["high_usd"] >= 3.55
    assert result["graph_estimate_provenance"]["planning_profile_count"] == 425
    assert result["graph_estimate_provenance"][
        "graph_neighborhood_profile_count"
    ] == 425


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
    assert result["graph_calls"]["expected"] == 37
    assert result["source_cost_usd"]["expected_usd"] == 0
    assert result["provider_calls"] == 0
