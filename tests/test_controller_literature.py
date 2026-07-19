from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from auto_zettelkasten.api import run_map
from auto_zettelkasten.controller import LocalController
from auto_zettelkasten.models import MapRequest

from conftest import FakeReader, FakeZotero


def test_local_controller_accepts_only_high_confidence_mechanical_proposals() -> None:
    controller = LocalController()
    proposals = [
        {"proposal_id": "a", "original_tag": " Exact Tag ", "proposed_tag": "exact-tag", "proposal_kind": "mechanical_normalization", "confidence": 0.95},
        {"proposal_id": "b", "original_tag": "Theory", "proposed_tag": "institutions", "proposal_kind": "semantic_merge", "confidence": 0.99},
        {"proposal_id": "c", "original_tag": "", "proposed_tag": "bad", "proposal_kind": "mechanical_normalization", "confidence": 1.0},
    ]
    decisions = controller.review_tag_proposals(proposals)
    assert [row["decision"] for row in decisions] == ["accepted", "parked", "rejected"]


def test_parked_tag_proposals_do_not_block_independent_semantic_clustering(tmp_path: Path, sample_items) -> None:
    class ParkingController:
        def review_tag_proposals(self, proposals: Sequence[Mapping[str, Any]]) -> Sequence[Mapping[str, Any]]:
            return [{**dict(row), "decision": "parked", "decision_reason": "integration_review_required"} for row in proposals]

    report = run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1"),
        client=FakeZotero(sample_items),
        reader=FakeReader(),
        controller=ParkingController(),
        run_id="parked-tags",
    )
    assert report.validated_note_count == 2
    assert report.cluster_map["clusters"] == []
    assert report.cluster_map["topic_neighborhood_count"] == 0
    assert report.cluster_map["navigation"]["promoted_subject_tag_count"] == 0
    assert report.gap_map["gap_candidates"] == []


def test_singleton_cluster_is_rejected(tmp_path: Path, sample_items) -> None:
    report = run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1"),
        client=FakeZotero(sample_items[:1]),
        reader=FakeReader(),
        run_id="singleton",
    )
    assert report.cluster_map["clusters"] == []
    assert report.cluster_map["unclustered_sources"][0]["reason"] == "no_comparable_multi_source_proposition"
    assert report.gap_map["gap_candidates"] == []
    compatible = yaml.safe_load((tmp_path / "02_source_memory" / "indexes" / "gap_candidates.yml").read_text())
    assert compatible["status"] == "complete_no_qualifying_gaps"
    assert compatible["gap_candidates"] == []


def test_controller_cannot_tamper_with_immutable_tag_proposal_fields(tmp_path: Path, sample_items) -> None:
    class TamperingController:
        def review_tag_proposals(self, proposals):
            return [
                {
                    "proposal_id": row["proposal_id"],
                    "note_id": "tampered-note",
                    "original_tag": "tampered-original",
                    "proposed_tag": "tampered-normalized",
                    "decision": "accepted",
                    "decision_reason": "attempted tampering",
                }
                for row in proposals
            ]

    report = run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1"),
        client=FakeZotero(sample_items[:1]),
        reader=FakeReader(),
        controller=TamperingController(),
        run_id="tamper",
    )
    assert report.validated_note_count == 1
    proposals = __import__("yaml").safe_load((tmp_path / "02_source_memory" / "indexes" / "tag_proposals.yml").read_text())["proposals"]
    assert {row["original_tag"] for row in proposals} == {"Shared Topic", "Exact Tag CASE"}
    assert {row["proposed_tag"] for row in proposals} == {"shared-topic", "exact-tag-case"}
    assert {row["note_id"] for row in proposals} == {report.items[0]["note_id"]}
