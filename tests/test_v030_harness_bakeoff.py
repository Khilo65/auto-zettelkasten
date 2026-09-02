from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

from auto_zettelkasten.files import read_yaml, sha256_file, write_yaml
from auto_zettelkasten.notes import (
    _public_note_text,
    _write_note_metadata,
    canonical_source_note_text,
    parse_atomic_note,
    render_atomic_note,
    semantic_note_hash,
    validate_note,
)


TOOLS = Path(__file__).parents[1] / "tools"


def load_tool(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _note(source_id: str, note_id: str, zotero_key: str) -> str:
    frontmatter = {
        "note_id": note_id,
        "source_id": source_id,
        "note_status": "analytical_atomic_note",
        "source_scope": "full_document",
        "source_coverage": {"gate": "passed"},
        "zotero_item_key": zotero_key,
        "source_file": f"zotero://{zotero_key}",
        "inspected_content_hash": "a" * 64,
        "content_route": "zotero_indexed_content",
        "reader_provider": "frozen",
        "reader_model": "frozen",
        "original_zotero_tags": [],
        "normalized_tags": ["peace"],
        "related_notes": [],
        "title": f"Synthetic source {zotero_key}",
        "creators": [{"lastName": "Researcher"}],
        "date": "2020",
    }
    analysis = {
        "thesis": "Research question: How do institutions shape peace?\nTheory: institutional bargaining.",
        "method_and_research_design": "Method: comparative case analysis.\nCases: A and B.",
        "evidence_and_data": "Data source: archival records and interviews.",
        "detailed_findings": "- Institutional bargaining shaped implementation in the cases; see p. 14.",
        "plain_english_interpretation": "- The cases associate institutional bargaining with implementation.",
        "strengths_and_contributions": "The comparison connects institutional design to implementation.",
        "methodological_critique": "Case selection limits broad causal inference.",
        "limitations": "- The study does not cover every post-conflict setting.",
        "what_this_source_can_support": "A case-grounded account of institutional bargaining.",
        "what_this_source_cannot_support": "A universal causal estimate.",
        "locators": "p. 14.",
    }
    text = render_atomic_note(frontmatter, analysis)
    assert validate_note(text).passed
    return text


def _origin(tmp_path: Path, tool) -> tuple[Path, Path, dict]:
    origin = tmp_path / "activated"
    rows = []
    definitions = [
        ("A001", "S01", "baseline", "PACKET-A"),
        ("A002", "S01", "delta", "PACKET-A"),
        ("B001", "S02", "baseline", "PACKET-B1"),
        ("B002", "S02", "delta", "PACKET-B2"),
    ]
    for key, stratum_id, phase, packet_key in definitions:
        source_id = f"source-zotero-{key.casefold()}"
        note_id = f"note-{key.casefold()}"
        note_path = Path("02_source_memory/notes") / f"{note_id}.md"
        profile_path = Path("02_source_memory/profiles") / f"{note_id}.yml"
        note = origin / note_path
        profile = origin / profile_path
        note.parent.mkdir(parents=True, exist_ok=True)
        profile.parent.mkdir(parents=True, exist_ok=True)
        internal = _note(source_id, note_id, key)
        frontmatter, _ = parse_atomic_note(internal)
        public = _public_note_text(canonical_source_note_text(internal), frontmatter)
        note.write_text(public, encoding="utf-8")
        _write_note_metadata(origin, note, frontmatter, machine_text=public)
        profile.write_text(f"profile_schema_version: '1'\nsource_id: {source_id}\n", encoding="utf-8")
        rows.append(
            {
                "source_id": source_id,
                "canonical_source_id": source_id,
                "zotero_key": key,
                "note_id": note_id,
                "phase": phase,
                "primary_stratum_id": stratum_id,
                "deepest_leaf_packet_key": packet_key,
                "note_path": str(note_path),
                "semantic_note_sha256": semantic_note_hash(note.read_text(encoding="utf-8")),
                "profile_path": str(profile_path),
                "profile_sha256": sha256_file(profile),
                "bundle_path": "",
                "bundle_sha256": "",
            }
        )
    selection = {
        "schema_version": "1",
        "benchmark_id": "synthetic",
        "quotas": [
            {"stratum_id": "S01", "combined_count": 2, "baseline_count": 1, "delta_count": 1},
            {"stratum_id": "S02", "combined_count": 2, "baseline_count": 1, "delta_count": 1},
        ],
        "cohesion": {
            "globally_complete_packet_keys": ["PACKET-A", "PACKET-B1", "PACKET-B2"]
        },
        "sources": rows,
    }
    selection["selection_sha256"] = tool.stable_key(
        json.dumps(selection, sort_keys=True, ensure_ascii=False)
    )
    selection_path = tmp_path / "selection.yml"
    write_yaml(selection_path, selection)

    write_yaml(
        origin / "01_custody/zotero/collection_snapshot.yml",
        {
            "schema_version": "1",
            "items": [
                {"key": key, "collection_keys": [stratum_id]}
                for key, stratum_id, _, _ in definitions
            ],
            "collections": [
                {"key": "S01", "parent_key": "", "name": "One"},
                {"key": "S02", "parent_key": "", "name": "Two"},
            ],
        },
    )
    write_yaml(
        origin / "02_source_memory/indexes/literature_positions.yml",
        {
            "literature_position_registry_schema_version": "2",
            "positions": [
                {
                    "literature_position_id": "inside",
                    "current_source_id": "source-zotero-a001",
                    "matched_source_id": "source-zotero-a002",
                    "match_candidates": ["source-zotero-a002"],
                },
                {
                    "literature_position_id": "outside",
                    "current_source_id": "source-zotero-a001",
                    "matched_source_id": "source-zotero-b001",
                    "match_candidates": ["source-zotero-b001"],
                },
            ],
        },
    )
    write_yaml(
        origin / "02_source_memory/indexes/missing_sources.yml",
        {
            "missing_source_registry_schema_version": "1",
            "sources": [
                {
                    "external_source_id": "external-1",
                    "discussed_by_source_ids": ["source-zotero-a001", "source-zotero-b001"],
                    "source_id": "source-zotero-b001",
                    "note_id": "note-b001",
                    "zotero_key": "B001",
                    "relevant_clusters": ["old-cluster"],
                }
            ],
        },
    )
    receipt = {
        "schema_version": "1",
        "selection_sha256": selection["selection_sha256"],
        "delta_count": 2,
        "activated": True,
    }
    write_yaml(origin / "evaluation/v030-delta-activation.yml", receipt)
    write_yaml(
        origin / "evaluation/v030-materialization.yml",
        {**receipt, "baseline_count": 2},
    )
    return origin, selection_path, selection


def _files(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_prepare_is_clean_filtered_and_byte_deterministic(tmp_path, monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(TOOLS))
    tool = load_tool("v030_prepare_harness_bakeoff")
    origin, selection_path, _ = _origin(tmp_path, tool)
    first = tmp_path / "first"
    second = tmp_path / "second"

    manifest = tool.prepare(origin, selection_path, first, ["S01"])
    tool.prepare(origin, selection_path, second, ["S01"])
    tool.validate(origin, selection_path, first, ["S01"])

    assert manifest["source_count"] == 2
    assert manifest["baseline_count"] == manifest["delta_count"] == 1
    assert _files(first) == _files(second)
    assert not (first / "evaluation").exists()
    assert not any(path.is_file() for path in (first / "03_literature_synthesis").rglob("*"))
    positions = read_yaml(first / "02_source_memory/indexes/literature_positions.yml", {})
    assert positions["positions"][1]["matched_source_id"] == ""
    missing = read_yaml(first / "02_source_memory/indexes/missing_sources.yml", {})
    assert missing["sources"][0]["discussed_by_source_ids"] == ["source-zotero-a001"]
    assert missing["sources"][0]["source_id"] == ""

    leak = first / "evaluation/reference.yml"
    leak.parent.mkdir()
    leak.write_text("leak: true\n", encoding="utf-8")
    with pytest.raises(ValueError, match="file inventory mismatch"):
        tool.validate(origin, selection_path, first, ["S01"])


def test_prepare_fails_closed_on_activation_strata_and_delta(tmp_path, monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(TOOLS))
    tool = load_tool("v030_prepare_harness_bakeoff")
    origin, selection_path, selection = _origin(tmp_path, tool)

    with pytest.raises(ValueError, match="unknown primary strata"):
        tool.selected_rows(selection, ["S99"])

    no_delta = deepcopy(selection)
    no_delta["sources"] = [
        row
        for row in no_delta["sources"]
        if not (row["primary_stratum_id"] == "S01" and row["phase"] == "delta")
    ]
    no_delta["quotas"][0].update(combined_count=1, delta_count=0)
    no_delta.pop("selection_sha256")
    no_delta["selection_sha256"] = tool.stable_key(
        json.dumps(no_delta, sort_keys=True, ensure_ascii=False)
    )
    with pytest.raises(ValueError, match="omits required delta"):
        tool.selected_rows(no_delta, ["S01"])

    receipt_path = origin / "evaluation/v030-delta-activation.yml"
    receipt = read_yaml(receipt_path, {})
    receipt["activated"] = False
    write_yaml(receipt_path, receipt)
    with pytest.raises(ValueError, match="not the activated"):
        tool.prepare(origin, selection_path, tmp_path / "target", ["S01"])


def test_packet_sample_is_whole_stratified_and_deterministic(tmp_path, monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(TOOLS))
    tool = load_tool("v030_prepare_harness_bakeoff")
    origin, selection_path, _ = _origin(tmp_path, tool)
    first = tmp_path / "first"
    second = tmp_path / "second"
    options = {"target_count": 3, "tolerance": 0, "seed": "synthetic-seed"}

    manifest = tool.prepare_packet_sample(origin, selection_path, first, **options)
    tool.prepare_packet_sample(origin, selection_path, second, **options)
    tool.validate_packet_sample(origin, selection_path, first, **options)

    assert manifest["source_count"] == 3
    assert manifest["sampling"]["actual_count"] == 3
    assert manifest["sampling"]["per_stratum_counts"] == {"S01": 2, "S02": 1}
    assert "PACKET-A" in manifest["sampling"]["selected_packet_keys"]
    assert _files(first) == _files(second)
    assert {row["deepest_leaf_packet_key"] for row in manifest["sources"]} == set(
        manifest["sampling"]["selected_packet_keys"]
    )
