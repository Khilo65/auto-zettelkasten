from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


@dataclass(slots=True)
class LocalController:
    """Conservative standalone controller for mechanical tag normalization."""

    acceptance_threshold: float = 0.9

    def review_tag_proposals(
        self,
        proposals: Sequence[Mapping[str, Any]],
    ) -> Sequence[Mapping[str, Any]]:
        reviewed: list[dict[str, Any]] = []
        for proposal in proposals:
            row = dict(proposal)
            confidence = float(row.get("confidence", 0) or 0)
            original = str(row.get("original_tag", "")).strip()
            proposed = str(row.get("proposed_tag", "")).strip()
            if not original or not proposed:
                row.update(decision="rejected", decision_reason="empty_tag")
            elif row.get("proposal_kind") == "mechanical_normalization" and confidence >= self.acceptance_threshold:
                row.update(decision="accepted", decision_reason="high_confidence_mechanical_normalization")
            else:
                row.update(decision="parked", decision_reason="requires_human_or_integrating_controller_review")
            reviewed.append(row)
        return reviewed
