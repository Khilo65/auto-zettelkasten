from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

import yaml

from auto_zettelkasten.indexes import (
    SOURCE_CATALOGUE_SCHEMA_VERSION,
    build_source_catalogue,
)


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
    cluster_catalogue = yaml.safe_load(
        Path(result["cluster_catalogue_path"]).read_text(encoding="utf-8")
    )
    assert result["source_count"] == 2
    assert result["literature_count"] == 2
    assert result["shard_count"] == 2
    assert SOURCE_CATALOGUE_SCHEMA_VERSION == "7"
    assert catalogue["schema_version"] == "7"
    assert cluster_catalogue["schema_version"] == "7"
    assert catalogue["revision_hash"] == result["revision_hash"]
    assert {row["title"] for row in catalogue["literatures"]} == {"Mediation", "Conflict relapse"}
    assert all(len(row["facets"]) <= 3 for row in catalogue["sources"])
    assert all(row["cluster_ids"] == ["cluster-bridge"] for row in catalogue["sources"])
    assert all(row["navigation"]["title"] == row["title"] for row in catalogue["sources"])
    assert all(row["identity"]["year"] == row["year"] for row in catalogue["sources"])
    master = Path(result["master_index_path"]).read_text(encoding="utf-8")
    assert "Mediation (1)" in master
    assert "Conflict relapse (1)" in master
    assert "Mediation and Peace" not in master
    mediation_shard = next(Path(path) for path in result["shard_paths"] if "mediation" in path)
    shard_text = mediation_shard.read_text(encoding="utf-8")
    assert "Fortna 2024 — Mediation and Peace" in shard_text
    assert "Thesis: Monitoring reduces uncertainty." in shard_text
    assert "Method: Comparative case analysis." in shard_text


def test_build_source_catalogue_upgrades_schema_two_locally_and_replays_stably(
    tmp_path: Path,
) -> None:
    _source_set(tmp_path, "lit", "Literature", ["s1"], ["n1"])
    catalogue_path = (
        tmp_path / "02_source_memory" / "indexes" / "source_catalogue.yml"
    )
    catalogue_path.write_text(
        json.dumps({"schema_version": "2", "sources": []}) + "\n",
        encoding="utf-8",
    )
    profile = {
        "source_id": "s1",
        "note_id": "n1",
        "concepts": ["mediation"],
    }
    note = _note("s1", "n1", "Study", "Author", "A compact thesis.")

    upgraded = build_source_catalogue(tmp_path, [profile], [note])
    upgraded_bytes = catalogue_path.read_bytes()
    replay = build_source_catalogue(tmp_path, [profile], [note])

    assert yaml.safe_load(upgraded_bytes)["schema_version"] == "7"
    assert str(catalogue_path) in upgraded["changed_paths"]
    assert replay["changed_paths"] == []
    assert catalogue_path.read_bytes() == upgraded_bytes


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


def test_downstream_graph_outputs_do_not_invalidate_relationship_routing(
    tmp_path: Path,
) -> None:
    source_id = "source-zotero-abcd1234"
    _source_set(tmp_path, "lit", "Literature", [source_id], ["n1"])
    profile = {
        "source_id": source_id,
        "note_id": "n1",
        "concepts": ["mediation"],
    }
    note = _note(
        source_id,
        "n1",
        "Mediation study",
        "Author",
        "A compact thesis.",
    )
    cluster = {
        "cluster_id": "cluster-a",
        "source_ids": [source_id],
        "shared_question": "What changes mediation outcomes?",
    }

    first = build_source_catalogue(
        tmp_path,
        [profile],
        [note],
        [{**cluster, "refresh_pending": False}],
    )
    second = build_source_catalogue(
        tmp_path,
        [profile],
        [note],
        [
            {
                **cluster,
                "cluster_id": "cluster-b",
                "shared_question": "A completely different downstream synthesis.",
                "refresh_pending": True,
            }
        ],
    )
    typed_links = tmp_path / "02_source_memory" / "indexes" / "typed_links.yml"
    typed_links.write_text(
        yaml.safe_dump(
            {
                "links": [
                    {
                        "relation_id": "relationship-new",
                        "source_id": source_id,
                        "target_source_id": "source-zotero-efgh5678",
                        "relation_type": "supports",
                        "confidence": 0.9,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    third = build_source_catalogue(
        tmp_path,
        [profile],
        [note],
        [],
    )
    catalogue = yaml.safe_load(Path(second["catalogue_path"]).read_text())

    assert second["revision_hash"] != first["revision_hash"]
    assert second["routing_revision_hash"] == first["routing_revision_hash"]
    assert third["routing_revision_hash"] == first["routing_revision_hash"]
    assert catalogue["sources"][0]["zotero_key"] == "ABCD1234"


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


def test_catalogue_exposes_machine_identity_without_rendering_it_to_navigation_shards(
    tmp_path: Path,
) -> None:
    note = {
        **_note(
            "source-zotero-abcd1234",
            "n1",
            "Mediation and Peace",
            "Fortna",
            "Monitoring reduces uncertainty.",
        ),
        "zotero_item_key": "ABCD1234",
        "zotero_item_keys": ["ABCD1234", "WXYZ9876"],
        "DOI": "10.1234/Example",
        "ISBN": "978-1-23456-789-0",
        "url": "https://example.test/study",
        "zotero_relations": {"dc:relation": ["WXYZ9876"]},
    }
    result = build_source_catalogue(
        tmp_path,
        [
            {
                "source_id": "source-zotero-abcd1234",
                "note_id": "n1",
                "mechanisms": ["monitoring"],
                "outcomes": ["peace duration"],
            }
        ],
        [note],
    )
    catalogue = yaml.safe_load(Path(result["catalogue_path"]).read_text())
    source = catalogue["sources"][0]

    assert source["identity"] == {
        "canonical_zotero_key": "ABCD1234",
        "zotero_item_keys": ["ABCD1234", "WXYZ9876"],
        "doi": "10.1234/example",
        "isbn": "9781234567890",
        "url": "https://example.test/study",
        "normalized_title": "mediation and peace",
        "normalized_author_surnames": ["fortna"],
        "year": "2024",
        "zotero_relations": {"dc:relation": ["WXYZ9876"]},
    }
    assert source["navigation"]["thesis"] == source["thesis"]
    rendered = "\n".join(
        Path(path).read_text(encoding="utf-8")
        for path in [*result["shard_paths"], *result["virtual_shard_paths"]]
    )
    assert "10.1234/example" not in rendered
    assert "9781234567890" not in rendered


def test_virtual_topic_indexes_are_bounded_overlapping_and_replay_stable(
    tmp_path: Path,
) -> None:
    profiles = [
        {
            "source_id": f"s{index}",
            "note_id": f"n{index}",
            "concepts": concepts,
            "mechanisms": mechanisms,
            "outcomes": outcomes,
        }
        for index, concepts, mechanisms, outcomes in (
            (1, ["peacekeeping"], ["monitoring"], ["peace duration"]),
            (2, ["peacekeeping"], ["monitoring"], ["peace duration"]),
            (3, ["recurrence"], ["commitment problems"], ["peace duration"]),
            (4, ["recurrence"], ["commitment problems"], ["peace duration"]),
            (5, ["conflict"], [], []),
        )
    ]
    notes = [
        _note(f"s{index}", f"n{index}", f"Study {index}", f"Author{index}", f"Thesis {index}.")
        for index in range(1, 6)
    ]

    first = build_source_catalogue(tmp_path, profiles, notes)
    catalogue = yaml.safe_load(Path(first["catalogue_path"]).read_text())
    virtual_paths = [Path(path) for path in first["virtual_shard_paths"]]
    memberships = {
        row["source_id"]: row["navigation"]["virtual_topic_ids"]
        for row in catalogue["sources"]
    }

    assert first["virtual_shard_count"] >= 4
    assert all(path.parent.name == "by_topic" for path in virtual_paths)
    assert all(path.stat().st_size <= 36_000 for path in virtual_paths)
    assert 1 < len(memberships["s1"]) <= 3
    assert memberships["s5"] == ["topic-catch-all"]
    assert not any("conflict" in path.stem for path in virtual_paths)
    assert Path(first["virtual_index_path"]).is_file()

    before = {path: path.read_bytes() for path in virtual_paths}
    replay = build_source_catalogue(tmp_path, profiles, notes)
    assert replay["changed_paths"] == []
    assert {path: path.read_bytes() for path in virtual_paths} == before
