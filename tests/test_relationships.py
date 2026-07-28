from __future__ import annotations

from pathlib import Path
from typing import Any

from auto_zettelkasten.files import read_yaml
from auto_zettelkasten.relationships import (
    candidate_rows,
    persist_relationship_registry,
    projected_related_links,
    validate_decisions,
    validate_verifications,
)


def _profile(
    source_id: str,
    *,
    note_status: str = "analytical_atomic_note",
    coverage: str = "full_text",
) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "note_id": f"note-{source_id.lower()}",
        "title": f"Source {source_id}",
        "context": {"note_status": note_status},
        "evidence_anchors": [
            {
                "evidence_anchor_id": f"anchor-{source_id.lower()}",
                "claim": f"Substantive claim from {source_id}",
                "locator": "p. 10",
                "support_envelope": {
                    "support_status": "supported",
                    "coverage": coverage,
                },
            }
        ],
    }


def _decision(
    source_id: str,
    target_id: str,
    *,
    relation_type: str = "supports",
    target_anchor_id: str | None = None,
) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "target_source_id": target_id,
        "status": "accepted",
        "relation_type": relation_type,
        "source_evidence_anchor_id": f"anchor-{source_id.lower()}",
        "target_evidence_anchor_id": (
            target_anchor_id or f"anchor-{target_id.lower()}"
        ),
        "comparison_unit": "shared proposition",
        "reason": "Both sources independently support the same substantive proposition.",
        "confidence": 0.9,
        "model": "test-model",
    }


def _verified_relations(
    decisions: list[dict[str, Any]],
    profiles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    verifications = [
        {
            **decision,
            "status": "confirmed",
            "source_evidence_anchor_id": decision["source_evidence"][
                "evidence_anchor_id"
            ],
            "target_evidence_anchor_id": decision["target_evidence"][
                "evidence_anchor_id"
            ],
            "requested_context": [],
        }
        for decision in decisions
    ]
    return validate_verifications(
        {"verifications": verifications},
        preliminary_decisions=decisions,
        profiles=profiles,
        verifier_provider="test-provider",
        verifier_model="test-model",
    )["accepted"]


def test_candidate_and_decision_validation_isolates_bad_rows() -> None:
    accepted_candidates, parked_candidates = candidate_rows(
        {
            "candidates": [
                {
                    "source_id": "A",
                    "target_kind": "source",
                    "target_id": "B",
                    "why_relevant": "They address the same proposition.",
                    "confidence": 0.9,
                },
                {
                    "source_id": "A",
                    "target_kind": "source",
                    "target_id": "MISSING",
                    "why_relevant": "Plausible but unavailable.",
                    "confidence": 0.8,
                },
                {
                    "source_id": "B",
                    "target_kind": "source",
                    "target_id": "C",
                    "why_relevant": "They use comparable evidence.",
                    "confidence": 0.7,
                },
            ]
        },
        focus_source_ids=["A", "B"],
        available_source_ids=["A", "B", "C"],
    )

    assert [(row["source_id"], row["target_id"]) for row in accepted_candidates] == [
        ("A", "B"),
        ("B", "C"),
    ]
    assert parked_candidates[0]["target_id"] == "MISSING"
    assert parked_candidates[0]["reason"] == "target_not_in_context"

    profiles = [_profile("A"), _profile("B"), _profile("C")]
    decisions = validate_decisions(
        {
            "decisions": [
                _decision("A", "B"),
                _decision("A", "C", target_anchor_id="not-in-profile"),
            ]
        },
        offered_pairs=[("A", "B"), ("A", "C")],
        profiles=profiles,
    )

    assert len(decisions["accepted"]) == 1
    assert decisions["accepted"][0]["source_evidence"]["locator"] == "p. 10"
    assert decisions["accepted"][0]["target_evidence"]["locator"] == "p. 10"
    assert len(decisions["parked"]) == 1
    assert "target_anchor_not_found" in decisions["parked"][0]["reason"]


def test_limited_abstract_anchor_is_allowed_but_metadata_anchor_is_parked() -> None:
    analytical = _profile("A")
    abstract_only = _profile(
        "B",
        note_status="abstract_only_atomic_note",
        coverage="abstract",
    )
    metadata_only = _profile(
        "C",
        note_status="metadata_only_source_note",
        coverage="metadata",
    )

    result = validate_decisions(
        {
            "decisions": [
                _decision("A", "B", relation_type="qualifies"),
                _decision("A", "C", relation_type="qualifies"),
            ]
        },
        offered_pairs=[("A", "B"), ("A", "C")],
        profiles=[analytical, abstract_only, metadata_only],
    )

    assert [(row["source_id"], row["target_source_id"]) for row in result["accepted"]] == [
        ("A", "B")
    ]
    assert len(result["parked"]) == 1
    assert "target_limited_scope_cannot_support_relation" in result["parked"][0][
        "reason"
    ]


def test_registry_is_idempotent_preserves_substance_and_repairs_compatibility(
    tmp_path: Path,
) -> None:
    profiles = [_profile("A"), _profile("B")]
    accepted = validate_decisions(
        {"decisions": [_decision("A", "B")]},
        offered_pairs=[("A", "B")],
        profiles=profiles,
    )["accepted"]
    accepted = _verified_relations(accepted, profiles)
    accepted[0].update(provider="test-provider", prompt_version="2")
    old_structural = {
        "relation_id": "structural-old",
        "source_id": "A",
        "target_source_id": "B",
        "relation_type": "cites",
        "active": True,
    }

    first = persist_relationship_registry(
        tmp_path,
        structural_relations=[old_structural],
        accepted_relations=accepted,
    )
    registry = Path(first["path"])
    compatibility = Path(first["compatibility_path"])
    original_registry_bytes = registry.read_bytes()
    original_compatibility_bytes = compatibility.read_bytes()

    replay = persist_relationship_registry(
        tmp_path,
        structural_relations=[old_structural],
        accepted_relations=accepted,
    )
    assert replay["revision_hash"] == first["revision_hash"]
    assert registry.read_bytes() == original_registry_bytes
    assert compatibility.read_bytes() == original_compatibility_bytes
    assert len(replay["pair_decisions"]) == 1
    assert replay["pair_decisions"][0]["status"] == "accepted"

    new_structural = {
        "relation_id": "structural-new",
        "source_id": "B",
        "target_source_id": "A",
        "relation_type": "zotero_related",
        "active": True,
    }
    refreshed = persist_relationship_registry(
        tmp_path,
        structural_relations=[new_structural],
    )
    active_ids = {row["relation_id"] for row in refreshed["links"]}
    assert accepted[0]["relation_id"] in active_ids
    assert "structural-new" in active_ids
    assert "structural-old" not in active_ids

    compatibility.write_text("corrupted: true\n", encoding="utf-8")
    repaired = persist_relationship_registry(
        tmp_path,
        structural_relations=[new_structural],
    )
    assert repaired["revision_hash"] == refreshed["revision_hash"]
    assert read_yaml(compatibility) == read_yaml(registry)


def test_projection_uses_reciprocal_relationship_type() -> None:
    profiles = [_profile("A"), _profile("B")]
    relation = validate_decisions(
        {"decisions": [_decision("A", "B")]},
        offered_pairs=[("A", "B")],
        profiles=profiles,
    )["accepted"]
    relation = _verified_relations(relation, profiles)

    from_a = projected_related_links(
        "A", profiles, relation, max_inferred_links=0
    )
    from_b = projected_related_links(
        "B", profiles, relation, max_inferred_links=0
    )

    assert from_a[0]["target_note_id"] == "note-b"
    assert from_a[0]["primary_relation_type"] == "supports"
    assert from_b[0]["target_note_id"] == "note-a"
    assert from_b[0]["primary_relation_type"] == "supported_by"


def test_unqualified_causal_reason_requires_causal_support_on_both_sides() -> None:
    decision = _decision("A", "B")
    decision["reason"] = (
        "Both studies show that the intervention causes the outcome to improve."
    )

    result = validate_decisions(
        {"decisions": [decision]},
        offered_pairs=[("A", "B")],
        profiles=[_profile("A"), _profile("B")],
    )

    assert result["accepted"] == []
    assert "unsupported_causal_upgrade" in result["parked"][0]["reason"]
