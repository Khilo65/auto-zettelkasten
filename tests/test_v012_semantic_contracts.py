from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from auto_zettelkasten.files import read_yaml, write_yaml
from auto_zettelkasten.indexes import (
    SOURCE_CATALOGUE_ROUTING_CARD_MAX_CHARS,
    build_source_catalogue,
)
from auto_zettelkasten.readers import (
    DeepSeekReader,
    _relationship_bridge_shard_system_prompt,
    _relationship_verification_system_prompt,
    _validate_relationship_response,
)
from auto_zettelkasten.relationships import (
    persist_relationship_registry,
    projected_related_links,
    validate_bridge_shard_pairs,
    validate_decisions,
    validate_verifications,
)


def _profile(source_id: str) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "note_id": f"note-{source_id.lower()}",
        "title": f"Source {source_id}",
        "context": {
            "note_status": "analytical_atomic_note",
            "source_scope": "full_document",
        },
        "methods": ["comparative analysis"],
        "mechanisms": ["credible commitment"],
        "outcomes": ["peace duration"],
        "cases": ["civil wars"],
        "populations": ["peace agreements"],
        "periods": ["1990-2020"],
        "datasets": ["agreement dataset"],
        "evidence_anchors": [
            {
                "evidence_anchor_id": f"anchor-{source_id.lower()}",
                "claim": f"Substantive claim from {source_id}",
                "locator": "p. 10",
                "support_envelope": {
                    "support_status": "supported",
                    "coverage": "full_text",
                },
            }
        ],
    }


def _tentative(source_id: str = "A", target_id: str = "B") -> dict[str, Any]:
    return {
        "source_id": source_id,
        "target_source_id": target_id,
        "status": "accepted",
        "relation_type": "supports",
        "comparison_unit": "shared proposition",
        "reason": "Both sources independently support the same substantive proposition.",
        "source_evidence_anchor_id": f"anchor-{source_id.lower()}",
        "target_evidence_anchor_id": f"anchor-{target_id.lower()}",
        "qualifiers": [],
        "confidence": 0.9,
    }


def _verified(
    tentative: dict[str, Any],
    profiles: list[dict[str, Any]],
    *,
    status: str = "confirmed",
    relation_type: str = "supports",
) -> dict[str, list[dict[str, Any]]]:
    row = {
        **tentative,
        "status": status,
        "relation_type": relation_type,
        "reason": "Both located claims independently establish the same bounded proposition.",
        "requested_context": [],
    }
    return validate_verifications(
        {"verifications": [row]},
        preliminary_decisions=[tentative],
        profiles=profiles,
        verifier_provider="test-provider",
        verifier_model="test-model",
    )


def test_verification_is_required_and_preserves_preliminary_lineage() -> None:
    profiles = [_profile("A"), _profile("B")]
    tentative = validate_decisions(
        {"decisions": [_tentative()]},
        offered_pairs=[("A", "B")],
        profiles=profiles,
    )["accepted"][0]

    assert tentative["verification_status"] == "pending"
    assert projected_related_links("A", profiles, [tentative], max_inferred_links=0) == []

    result = _verified(tentative, profiles)
    relation = result["accepted"][0]
    assert relation["verification_status"] == "confirmed"
    assert relation["preliminary_decision_hash"]
    assert relation["provenance"] == "probabilistic_relationship_verification"
    assert projected_related_links(
        "B", profiles, [relation], max_inferred_links=0
    )[0]["primary_relation_type"] == "supported_by"

    wrong = _verified(tentative, profiles, relation_type="qualifies")
    assert wrong["accepted"] == []
    assert "confirmed_decision_does_not_match_preliminary" in wrong["parked"][0][
        "reason"
    ]


def test_verified_negative_retires_same_hash_machine_edge_but_not_human(
    tmp_path: Path,
) -> None:
    profiles = [_profile("A"), _profile("B")]
    tentative = validate_decisions(
        {"decisions": [_tentative()]},
        offered_pairs=[("A", "B")],
        profiles=profiles,
    )["accepted"][0]
    relation = _verified(tentative, profiles)["accepted"][0]
    human = {
        "relation_id": "human-a-b",
        "source_id": "A",
        "target_source_id": "B",
        "relation_type": "supports",
        "provenance": "human_authored",
        "active": True,
    }
    persist_relationship_registry(
        tmp_path,
        structural_relations=[human],
        accepted_relations=[relation],
    )
    negative = _verified(
        tentative,
        profiles,
        status="no_relationship",
        relation_type="",
    )["no_relationship"][0]
    result = persist_relationship_registry(
        tmp_path,
        structural_relations=[human],
        no_relationship_decisions=[negative],
    )

    by_id = {row["relation_id"]: row for row in result["relations"]}
    assert by_id[relation["relation_id"]]["active"] is False
    assert by_id[relation["relation_id"]]["decision_status"] == "retired"
    assert by_id["human-a-b"]["active"] is True


def test_schema_two_machine_links_migrate_to_inactive_legacy_history(
    tmp_path: Path,
) -> None:
    path = tmp_path / "02_source_memory" / "indexes" / "typed_links.yml"
    compatibility = path.with_name("typed_note_links.yml")
    legacy = {
        "registry_schema_version": "2",
        "relations": [
            {
                "relation_id": "legacy-machine",
                "source_id": "A",
                "target_source_id": "B",
                "relation_type": "supports",
                "provenance": "probabilistic_relationship_adjudication",
                "active": True,
            },
            {
                "relation_id": "human-link",
                "source_id": "A",
                "target_source_id": "B",
                "relation_type": "qualifies",
                "provenance": "human_authored",
                "active": True,
            },
        ],
        "links": [],
        "pair_decisions": [],
    }
    write_yaml(path, legacy)
    write_yaml(compatibility, legacy)

    result = persist_relationship_registry(tmp_path, structural_relations=[])
    by_id = {row["relation_id"]: row for row in result["relations"]}
    assert read_yaml(path)["registry_schema_version"] == "4"
    assert by_id["legacy-machine"]["verification_status"] == "legacy_unverified"
    assert by_id["legacy-machine"]["active"] is False
    assert by_id["human-link"]["active"] is True


def test_bridge_shard_contract_rejects_same_literature_and_caps_pairs() -> None:
    shards = [
        {"shard_id": "a1", "literature_id": "a"},
        {"shard_id": "a2", "literature_id": "a"},
        {"shard_id": "b1", "literature_id": "b"},
    ]
    accepted, parked = validate_bridge_shard_pairs(
        {
            "shard_pairs": [
                {
                    "left_shard_id": "a1",
                    "right_shard_id": "b1",
                    "reason": "Their theses identify a plausible cross-literature mechanism.",
                    "confidence": 0.9,
                },
                {
                    "left_shard_id": "a1",
                    "right_shard_id": "a2",
                    "reason": "These shards happen to share several keywords.",
                    "confidence": 0.8,
                },
            ]
        },
        available_shards=shards,
    )
    assert [(row["left_shard_id"], row["right_shard_id"]) for row in accepted] == [
        ("a1", "b1")
    ]
    assert "same_literature_shard_pair" in parked[0]["reason"]


def test_catalogue_routing_cards_are_bounded_and_semantically_compact(
    tmp_path: Path,
) -> None:
    source_set = (
        tmp_path
        / "02_source_memory"
        / "indexes"
        / "source_sets"
        / "literature-a.yml"
    )
    write_yaml(
        source_set,
        {
            "source_set_id": "literature-a-snapshot",
            "source_set_alias": "literature-a",
            "source_set_type": "zotero_collection",
            "collection_name": "Mediation",
            "source_ids": ["A"],
            "note_ids": ["note-a"],
        },
    )
    profile = _profile("A")
    result = build_source_catalogue(
        tmp_path,
        [profile],
        [
            {
                "source_id": "A",
                "note_id": "note-a",
                "title": "Mediation and Peace",
                "date": "2024",
                "creators": [{"lastName": "Fortna"}],
                "thesis": "Monitoring reduces uncertainty after conflict.",
                "method": "Comparative analysis.",
            }
        ],
    )
    payload = read_yaml(Path(result["catalogue_path"]))
    source = payload["sources"][0]
    card = payload["shards"][0]["routing_card"]

    assert source["collections"] == ["Mediation"]
    assert source["source_scope"] == "full_document"
    assert source["evidence_coverage"] == "full_text"
    assert source["facets_by_type"]["dataset"] == ["agreement dataset"]
    assert len(card["representative_theses"]) <= 5
    assert (
        len(json.dumps(card, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        <= SOURCE_CATALOGUE_ROUTING_CARD_MAX_CHARS
    )


def test_builtin_reader_exposes_v2_bridge_and_verification_contracts() -> None:
    assert callable(getattr(DeepSeekReader, "select_relationship_bridge_shards"))
    assert callable(getattr(DeepSeekReader, "verify_relationships"))
    assert "relationship prompt v2" in _relationship_bridge_shard_system_prompt()
    assert "preliminary decision as a claim to audit" in (
        _relationship_verification_system_prompt()
    )
    assert _validate_relationship_response(
        {"shard_pairs": []}, kind="shard_pair_selection"
    ) == {"shard_pairs": []}
    assert _validate_relationship_response(
        {"verifications": []}, kind="relationship_verification"
    ) == {"verifications": []}
