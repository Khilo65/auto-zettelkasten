from __future__ import annotations

import importlib.util
from pathlib import Path


TOOLS = Path(__file__).parents[1] / "tools"


def load_tool(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mapping_sample_sanitizes_phase_boundaries(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(TOOLS))
    tool = load_tool("v030_prepare_mapping_sample")
    positions, hidden = tool.sanitize_positions(
        {
            "positions": [
                {
                    "literature_position_id": "position-1",
                    "current_source_id": "source-a",
                    "matched_source_id": "source-delta",
                    "matched_zotero_key": "DELTA",
                    "match_basis": "doi",
                    "match_confidence": 1.0,
                    "match_candidates": ["source-delta"],
                }
            ]
        },
        {"source-a"},
    )
    row = positions["positions"][0]
    assert row["match_status"] == "not_in_snapshot"
    assert row["matched_source_id"] == ""
    assert hidden == [{"literature_position_id": "position-1", "matched_source_id": "source-delta"}]

    missing, _ = tool.sanitize_missing(
        {
            "sources": [
                {
                    "external_source_id": "external-1",
                    "discussed_by_source_ids": ["source-a", "source-outside"],
                    "source_id": "source-delta",
                    "note_id": "note-delta",
                    "zotero_key": "DELTA",
                    "relevant_clusters": ["old-cluster"],
                }
            ]
        },
        {"source-a"},
    )
    row = missing["sources"][0]
    assert row["discussed_by_source_ids"] == ["source-a"]
    assert row["source_id"] == ""
    assert row["relevant_clusters"] == []


def test_diversity_counts_accepts_single_pass_iterables(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(TOOLS))
    tool = load_tool("v030_prepare_mapping_sample")
    info = {
        "a": {"item_category": "journal", "year_bucket": "pre_1990"},
        "b": {"item_category": "books", "year_bucket": "2020_plus"},
    }
    types, years = tool.diversity_counts((value for value in info), info)
    assert types == {"journal": 1, "books": 1}
    assert years == {"pre_1990": 1, "2020_plus": 1}


def test_analytical_fill_uses_at_most_one_boundary_packet(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(TOOLS))
    tool = load_tool("v030_prepare_mapping_sample")
    baseline = {}
    selected = set()
    pool = {f"source-{index}" for index in range(9)}
    packet_key = {
        source_id: "leaf-a" if index < 4 else "leaf-b"
        for index, source_id in enumerate(sorted(pool))
    }
    info = {source_id: {"scope_class": "analytical"} for source_id in pool}
    tool.fill_analytical_cell(
        seed="seed",
        stratum_id="stratum",
        target=7,
        baseline=baseline,
        selected=selected,
        pool=pool,
        packet_key=packet_key,
        global_packet_sizes={"leaf-a": 4, "leaf-b": 5},
        info=info,
    )
    boundary = {
        reason.split(":", 1)[1]
        for _, reason in baseline.values()
        if reason.startswith("boundary_packet:")
    }
    assert len(baseline) == 7
    assert len(boundary) <= 1


def test_activation_requires_exact_delta_count(tmp_path, monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(TOOLS))
    tool = load_tool("v030_prepare_mapping_sample")
    evaluation = tmp_path / "evaluation"
    evaluation.mkdir()
    from auto_zettelkasten.files import write_yaml

    write_yaml(
        evaluation / "v030-selection.yml",
        {"selection_sha256": "selection", "sources": [{"phase": "delta"}] * 99},
    )
    try:
        tool.activate_delta(tmp_path)
    except ValueError as exc:
        assert "exactly 100 delta rows" in str(exc)
    else:
        raise AssertionError("activation accepted an incomplete delta")
