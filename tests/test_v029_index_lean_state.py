from pathlib import Path
from itertools import combinations as stdlib_combinations

import auto_zettelkasten.literature as literature_module
import auto_zettelkasten.navigation as navigation_module
from auto_zettelkasten.api import estimate_cost
from auto_zettelkasten.files import read_yaml, write_yaml
from auto_zettelkasten.migration import (
    finalize_v029_lean_state,
    migrate_v029_lean_state,
)
from auto_zettelkasten.literature import (
    map_profile_relations,
    normalize_evidence_profiles,
)
from auto_zettelkasten.navigation import build_typed_source_relations
from auto_zettelkasten.pipeline import (
    _family_route_inventory,
    _namespace_literature_family_plan,
    _reconcile_overlapping_family_cards,
)


def _valid_receipt() -> dict[str, object]:
    return {
        "receipt_schema_version": "1",
        "engine_version": "0.29.1",
        "artifact_schema_version": "1.20",
        "identity": "semantic-build",
        "status": "built",
        "semantic_replayable": True,
    }


def test_family_routes_have_one_stable_primary_and_secondary_memberships() -> None:
    rows = [{"source_id": source_id} for source_id in "ABC"]
    hashes = {source_id: f"hash-{source_id}" for source_id in "ABC"}
    catalogue = {
        "virtual_shards": [
            {
                "shard_id": "topic-one",
                "topic_id": "topic-one",
                "source_ids": ["A", "B"],
            }
        ],
        "collections": [
            {
                "key": "child",
                "name": "Child",
                "direct_source_ids": ["B", "C"],
            }
        ],
        "shards": [{"shard_id": "all", "source_ids": ["A", "B", "C"]}],
    }

    primary, jobs, secondary = _family_route_inventory(
        rows,
        catalogue,
        prior_routes={"A": "literature:all"},
        prior_hashes={"A": "hash-A"},
        current_hashes=hashes,
    )

    assert primary == {
        "A": "literature:all",
        "B": "virtual:topic-one",
        "C": "collection:child",
    }
    assert sorted(source_id for job in jobs for source_id in job["source_ids"]) == [
        "A",
        "B",
        "C",
    ]
    assert "collection:child" in secondary["B"]
    assert "literature:all" in secondary["C"]


def test_family_packet_ids_are_namespaced_without_losing_dispositions() -> None:
    result = _namespace_literature_family_plan(
        {
            "literature_families": [
                {"family_id": "shared", "source_ids": ["A", "B"]}
            ],
            "discovery_jobs": [
                {"job_id": "discover", "family": "shared"}
            ],
            "source_dispositions": [
                {"source_id": "A", "family_ids": ["shared"]}
            ],
        },
        "packet-a",
    )

    assert result["literature_families"][0]["family_id"] == "packet-a:shared"
    assert result["discovery_jobs"][0] == {
        "job_id": "packet-a:discover",
        "family": "packet-a:shared",
    }
    assert result["source_dispositions"][0]["family_ids"] == [
        "packet-a:shared"
    ]


def test_custom_reasoner_without_reconciliation_capability_is_not_called() -> None:
    class Reasoner:
        capabilities = {}

    class Calls:
        def __call__(self, *_args, **_kwargs):
            raise AssertionError("custom reasoner must not receive a new call")

    plan = {
        "literature_families": [
            {"family_id": "one", "source_ids": ["A", "B"]},
            {"family_id": "two", "source_ids": ["B", "C"]},
        ]
    }
    result, warnings = _reconcile_overlapping_family_cards(
        plan,
        request=None,  # type: ignore[arg-type]
        reasoner=Reasoner(),  # type: ignore[arg-type]
        reasoner_calls=Calls(),  # type: ignore[arg-type]
    )

    assert result == plan
    assert warnings == []


def test_v029_cleanup_waits_for_durable_replacement_receipt(tmp_path: Path) -> None:
    legacy = tmp_path / "03_literature_synthesis" / "tag_concept_registry.yml"
    write_yaml(legacy, {"concepts": ["legacy"]})

    migrated = migrate_v029_lean_state(tmp_path)
    assert migrated["cleanup_pending"] is True
    assert legacy.is_file()

    receipt = tmp_path / "11_state" / "runs" / "run" / "semantic_build_receipt.yml"
    assert finalize_v029_lean_state(tmp_path, replacement_receipt=receipt) == []
    assert legacy.is_file()

    write_yaml(receipt, _valid_receipt())
    assert finalize_v029_lean_state(tmp_path, replacement_receipt=receipt) == [
        "03_literature_synthesis/tag_concept_registry.yml"
    ]
    assert not legacy.exists()


def test_v029_cleanup_preserves_a_changed_legacy_candidate(tmp_path: Path) -> None:
    legacy = tmp_path / "03_literature_synthesis" / "tag_concept_registry.yml"
    write_yaml(legacy, {"concepts": ["legacy"]})
    migrate_v029_lean_state(tmp_path)
    write_yaml(legacy, {"concepts": ["changed after migration"]})
    receipt = tmp_path / "11_state" / "semantic_build_receipt.yml"
    write_yaml(receipt, _valid_receipt())

    assert finalize_v029_lean_state(tmp_path, replacement_receipt=receipt) == []
    assert legacy.is_file()
    marker = read_yaml(
        tmp_path
        / "11_state"
        / "migrations"
        / "auto-zettelkasten-0.29-lean-index-state.yml"
    )
    assert marker["cleanup_pending"] is True


def test_v029_cleanup_rejects_partial_or_nonreplayable_receipts(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / "03_literature_synthesis" / "tag_concept_registry.yml"
    write_yaml(legacy, {"concepts": ["legacy"]})
    migrate_v029_lean_state(tmp_path)
    receipt = tmp_path / "11_state" / "semantic_build_receipt.yml"

    for override in (
        {"status": "partial"},
        {"semantic_replayable": False},
        {"identity": ""},
    ):
        write_yaml(receipt, {**_valid_receipt(), **override})
        assert finalize_v029_lean_state(
            tmp_path, replacement_receipt=receipt
        ) == []
        assert legacy.is_file()


def test_cost_estimate_is_local_and_records_pricing_provenance(tmp_path: Path) -> None:
    class Zotero:
        def inventory(self, scope: str, collection_key: str | None):
            assert (scope, collection_key) == ("library", None)
            return [{"key": "A"}, {"key": "B"}]

    result = estimate_cost(tmp_path, zotero_client=Zotero())  # type: ignore[arg-type]

    assert result["provider_calls"] == 0
    assert result["new_source_jobs"] == 2
    assert result["source_cost_usd"]["expected_usd"] > 0
    assert result["pricing"]["source"]
    assert result["pricing"]["effective_date"]
    assert read_yaml(Path(result["path"]))["provider_calls"] == 0


def test_cost_estimate_prices_graph_when_every_source_is_reusable(tmp_path: Path) -> None:
    class Zotero:
        def inventory(self, scope: str, collection_key: str | None):
            raise AssertionError("graph-only estimate must not inventory live Zotero")

    profile_dir = tmp_path / "02_source_memory" / "profiles"
    for index in range(250):
        write_yaml(profile_dir / f"source-{index}.yml", {"source_id": f"source-{index}"})
    write_yaml(
        tmp_path
        / "02_source_memory"
        / "indexes"
        / "source_sets"
        / "source-set-auto-zettelkasten-workspace.yml",
        {"inventory_count": 300, "validated_note_count": 10},
    )

    result = estimate_cost(
        tmp_path,
        graph_only=True,
        zotero_client=Zotero(),  # type: ignore[arg-type]
    )

    assert result["new_source_jobs"] == 0
    assert result["mode"] == "graph_only"
    assert result["inventory_count"] == 300
    assert result["graph_calls"]["expected"] == 6
    assert result["source_cost_usd"]["expected_usd"] == 0
    assert result["graph_calls"]["expected"] > 0
    assert result["graph_cost_usd"]["expected_usd"] > 0
    assert result["total_cost_usd"] == result["graph_cost_usd"]


def test_large_unrelated_corpora_do_not_enter_full_pair_loops(monkeypatch) -> None:
    profiles = [
        {
            "source_id": f"source-{index}",
            "note_id": f"note-{index}",
            "note_status": "analytical_atomic_note",
            "semantic_topic_scores": {f"topic-{index}": 1.0},
            "study_lineage": {"dataset_ids": [f"dataset-{index}"]},
        }
        for index in range(500)
    ]
    literature_group_sizes: list[int] = []

    def tracked_literature(values, length):
        rows = tuple(values)
        literature_group_sizes.append(len(rows))
        return stdlib_combinations(rows, length)

    monkeypatch.setattr(
        literature_module, "combinations", tracked_literature
    )
    assert map_profile_relations(profiles) == []
    normalize_evidence_profiles(profiles)
    assert max(literature_group_sizes, default=0) <= 1

    assignments = [
        {
            "source_id": f"source-{index}",
            "facet_type": "concept",
            "subject_tag_id": f"tag-{index}",
            "canonical_tag": f"concept/topic-{index}",
            "promotion_status": "promoted",
        }
        for index in range(500)
    ]
    navigation_group_sizes: list[int] = []

    def tracked_navigation(values, length):
        rows = tuple(values)
        navigation_group_sizes.append(len(rows))
        return stdlib_combinations(rows, length)

    monkeypatch.setattr(
        navigation_module, "combinations", tracked_navigation
    )
    assert build_typed_source_relations(
        profiles, tag_assignments=assignments
    ) == []
    assert max(navigation_group_sizes, default=0) <= 1
