from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

import yaml

from auto_zettelkasten.indexes import build_source_catalogue


@dataclass
class ProfileStub:
    source_id: str
    note_id: str
    dependency_hash: str
    methods: list[str]
    concepts: list[str]
    mechanisms: list[str]
    outcomes: list[str]
    cases: list[str]
    research_questions: list[str]
    findings: list[dict[str, str]]


def _source_set(root: Path, alias: str, title: str, source_ids: list[str], note_ids: list[str]) -> None:
    path = root / "02_source_memory" / "indexes" / "source_sets" / f"{alias}.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "source_set_id": f"{alias}-snapshot",
                "source_set_alias": alias,
                "source_set_type": "zotero_collection",
                "zotero_collection_key": alias,
                "collection_name": title,
                "source_ids": source_ids,
                "note_ids": note_ids,
                "latest_snapshot_id": f"{alias}-snapshot",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _note(source_id: str, note_id: str, title: str, author: str, thesis: str) -> dict[str, object]:
    return {
        "source_id": source_id,
        "note_id": note_id,
        "title": title,
        "date": "2024-05",
        "creators": [{"firstName": "A.", "lastName": author}],
        "thesis": thesis,
        "method": "Comparative case analysis. Further detail is intentionally omitted.",
        "note_path": f"02_source_memory/atomic_notes/{author}2024 - {title}.md",
    }


def test_build_source_catalogue_projects_profiles_into_collection_shards(tmp_path: Path) -> None:
    _source_set(tmp_path, "source-set-zotero-mediation", "Mediation", ["s1"], ["n1"])
    _source_set(tmp_path, "source-set-zotero-relapse", "Conflict relapse", ["s2"], ["n2"])
    profiles = [
        ProfileStub("s1", "n1", "hash-1", ["cases"], ["mediation"], ["monitoring"], ["peace"], ["Liberia"], [], []),
        {
            "source_id": "s2",
            "note_id": "n2",
            "dependency_hash": "hash-2",
            "methods": ["survival analysis"],
            "concepts": ["recurrence", "war termination"],
            "mechanisms": ["credible commitment"],
            "outcomes": ["peace duration"],
            "cases": ["civil wars"],
            "research_questions": [],
            "findings": [],
        },
    ]
    notes = [
        _note("s1", "n1", "Mediation and Peace", "Fortna", "Monitoring reduces uncertainty."),
        _note("s2", "n2", "Relapse after War", "Walter", "Commitment problems raise recurrence risk."),
    ]

    result = build_source_catalogue(
        tmp_path,
        profiles,
        notes,
        [{"cluster_id": "cluster-bridge", "source_ids": ["s1", "s2"]}],
    )

    catalogue = yaml.safe_load(Path(result["catalogue_path"]).read_text(encoding="utf-8"))
    assert result["source_count"] == 2
    assert result["literature_count"] == 2
    assert result["shard_count"] == 2
    assert catalogue["revision_hash"] == result["revision_hash"]
    assert {row["title"] for row in catalogue["literatures"]} == {"Mediation", "Conflict relapse"}
    assert all(len(row["facets"]) <= 3 for row in catalogue["sources"])
    assert all(row["cluster_ids"] == ["cluster-bridge"] for row in catalogue["sources"])
    master = Path(result["master_index_path"]).read_text(encoding="utf-8")
    assert "Mediation (1)" in master
    assert "Conflict relapse (1)" in master
    assert "Mediation and Peace" not in master
    mediation_shard = next(Path(path) for path in result["shard_paths"] if "mediation" in path)
    shard_text = mediation_shard.read_text(encoding="utf-8")
    assert "Fortna 2024 — Mediation and Peace" in shard_text
    assert "Thesis: Monitoring reduces uncertainty." in shard_text
    assert "Method: Comparative case analysis." in shard_text


def test_build_source_catalogue_is_byte_stable_and_rewrites_only_changed_shard(tmp_path: Path) -> None:
    _source_set(tmp_path, "lit-a", "Literature A", ["s1"], ["n1"])
    _source_set(tmp_path, "lit-b", "Literature B", ["s2"], ["n2"])
    profiles = {
        "s1": {
            "source_id": "s1",
            "note_id": "n1",
            "dependency_hash": "hash-1",
            "concepts": ["one"],
        },
        "s2": {
            "source_id": "s2",
            "note_id": "n2",
            "dependency_hash": "hash-2",
            "concepts": ["two"],
        },
    }
    notes = [
        _note("s1", "n1", "First", "Alpha", "First thesis."),
        _note("s2", "n2", "Second", "Beta", "Second thesis."),
    ]
    first = build_source_catalogue(tmp_path, profiles, notes)
    output_paths = [
        first["catalogue_path"],
        first["master_index_path"],
        *first["shard_paths"],
    ]
    before = {path: Path(path).read_bytes() for path in output_paths}

    replay = build_source_catalogue(tmp_path, profiles, notes)
    assert replay["revision_hash"] == first["revision_hash"]
    assert replay["changed_paths"] == []
    assert {path: Path(path).read_bytes() for path in before} == before

    changed_profiles = json.loads(json.dumps(profiles))
    changed_profiles["s1"]["concepts"] = ["changed"]
    changed_profiles["s1"]["dependency_hash"] = "hash-1b"
    changed = build_source_catalogue(tmp_path, changed_profiles, notes)
    changed_names = {Path(path).name for path in changed["changed_paths"]}
    assert "lit-a.md" in changed_names
    assert "lit-b.md" not in changed_names
    assert "source_catalogue.yml" in changed_names
    assert "INDEX.md" in changed_names


def test_large_catalogue_uses_bounded_unique_shards_and_prunes_stale_files(
    tmp_path: Path,
) -> None:
    source_ids = [f"s{index}" for index in range(300)]
    note_ids = [f"n{index}" for index in range(300)]
    _source_set(tmp_path, "large-lit", "Large Literature", source_ids, note_ids)
    profiles = [
        {
            "source_id": source_id,
            "note_id": note_id,
            "concepts": [f"unique facet {index}"],
            "methods": ["comparative analysis"],
        }
        for index, (source_id, note_id) in enumerate(zip(source_ids, note_ids))
    ]
    notes = [
        _note(
            source_id,
            note_id,
            f"Study {index}",
            f"Author{index:03d}",
            "A bounded thesis sentence that remains compact but makes the large shard split realistic.",
        )
        for index, (source_id, note_id) in enumerate(zip(source_ids, note_ids))
    ]

    result = build_source_catalogue(tmp_path, profiles, notes)
    catalogue = yaml.safe_load(Path(result["catalogue_path"]).read_text())
    shard_paths = [Path(path) for path in result["shard_paths"]]

    assert 1 < len(shard_paths) < 20
    assert len(shard_paths) == len(set(shard_paths))
    assert sum(row["source_count"] for row in catalogue["shards"]) == 300
    assert all(row["shard_id"] for row in catalogue["shards"])
    assert all(row["source_ids"] for row in catalogue["shards"])
    assert len(Path(result["master_index_path"]).read_text()) < 16_000

    old_shards = set(shard_paths)
    collapsed = build_source_catalogue(tmp_path, profiles[:2], notes[:2])
    assert collapsed["shard_count"] == 1
    assert all(not path.exists() for path in old_shards - {Path(collapsed["shard_paths"][0])})
