"""Private comparison adapter; production planner semantics remain unchanged."""

from __future__ import annotations

from pathlib import Path
from types import FunctionType
from typing import Any, Mapping, Sequence

from auto_zettelkasten import pipeline
from auto_zettelkasten.literature import _CheckpointedReasonerCalls
from auto_zettelkasten.models import LiteratureMapRequest


def experiment_request(workspace: Path, *, model: str, **kwargs: Any) -> LiteratureMapRequest:
    """Permit Luna only in this private experiment, retaining request validation."""
    if model not in {"gpt-5.6-terra", "gpt-5.6-luna"}:
        raise ValueError("experimental linking model must be Terra or Luna")
    request = LiteratureMapRequest(
        workspace, provider="codex", model="gpt-5.6-terra",
        reasoning_effort="max", provider_concurrency=1, **kwargs,
    )
    # The public literature-model allowlist stays unchanged. All serialized
    # requests and receipts identify the actual experimental model.
    object.__setattr__(request, "model", model)
    return request


class ExperimentReasonerCalls(_CheckpointedReasonerCalls):
    """Production checkpoints with experiment-only capacity and no prefix recovery."""

    def __init__(self, workspace: Path, run_id: str, reader: Any, request: Any,
                 *, experiment_identity: Mapping[str, Any], input_char_budget: int) -> None:
        if reader.name != "codex" or request.provider != "codex":
            raise ValueError("live comparison calls require the subscription adapter")
        if request.reasoning_effort != "max" or reader.reasoning_effort != "max":
            raise ValueError("experimental calls require maximum reasoning")
        if request.provider_concurrency != 1:
            raise ValueError("experimental graph concurrency must be one")
        if not experiment_identity or input_char_budget < 1:
            raise ValueError("experiment identity and input allowance are required")
        super().__init__(workspace, run_id, reader, request, retry_terminal_failures=False)
        self.experiment_identity = dict(experiment_identity)
        original = _CheckpointedReasonerCalls.__call__
        self._experiment_call = FunctionType(
            original.__code__,
            {**original.__globals__,
             "_reasoner_context_char_budget": lambda _reader, _request: input_char_budget,
             "_recover_candidate_prefix": lambda *_args, **_kwargs: None},
            original.__name__, original.__defaults__, original.__closure__,
        )
        self._experiment_call.__kwdefaults__ = original.__kwdefaults__

    def __call__(self, stage: str, key: str, method_name: str,
                 profiles: Sequence[Any], context: Mapping[str, Any]) -> Mapping[str, Any]:
        if method_name not in {"plan_literature_families", "select_relationship_candidates", "select_direct_candidates"}:
            raise ValueError("comparison permits only family planning and linking calls")
        return self._experiment_call(self, stage, key, method_name, profiles, {
            **context, "linking_experiment_identity": self.experiment_identity,
        })


def _experimental_function(function: Any, *, max_records: int, input_char_budget: int) -> Any:
    # Isolated globals avoid changing another reader's limits in this process.
    adapted = FunctionType(
        function.__code__,
        {
            **function.__globals__,
            "_RELATIONSHIP_DISCOVERY_PAGE_SIZE": max_records,
            "_relationship_context_char_budget": lambda _reader, _request: input_char_budget,
        },
        function.__name__,
        function.__defaults__,
        function.__closure__,
    )
    adapted.__kwdefaults__ = function.__kwdefaults__
    return adapted


def run_planner(
    workspace: Path,
    *,
    profiles: Sequence[Any],
    catalogue: Mapping[str, Any],
    source_set: Mapping[str, Any],
    note_rows: Sequence[Mapping[str, Any]],
    reader: Any,
    request: Any,
    reasoner_calls: Any,
    max_records: int,
    input_char_budget: int,
) -> dict[str, Any]:
    """Plan and link only, using caller-owned receipts, budgets and final persistence.

    The transport must admit the complete serialized request; this character
    allowance is only the existing planner's preliminary partitioning bound.
    Raising the output cap also raises production's bounded family-completion
    threshold. Its routes, quotas, breadth pass and eligibility rules are kept.
    """
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 1
           for value in (max_records, input_char_budget)):
        raise ValueError("experimental capacities must be positive integers")
    if request.reasoning_effort != "max" or reader.reasoning_effort != "max":
        raise ValueError("experimental planner requires explicit maximum reasoning")
    if request.provider_concurrency != 1:
        raise ValueError("experimental graph concurrency must be one")
    if getattr(reader, "ordinary_relationship_decision_contract", "") != "relationship-decision-v11":
        raise ValueError("experimental planner requires single-call ordinary decisions")
    options = {"max_records": max_records, "input_char_budget": input_char_budget}
    family_plan = _experimental_function(pipeline._plan_literature_families, **options)(
        workspace, profiles=profiles, catalogue=catalogue, reasoner=reader,
        reasoner_calls=reasoner_calls, request=request,
    )
    if family_plan is None:
        raise ValueError("experimental planner produced no family plan; fallback routing is disabled")
    relationships = _experimental_function(pipeline._run_relationship_reasoning, **options)(
        workspace, profiles=profiles, source_set=source_set, catalogue=catalogue,
        reasoner=reader, reasoner_calls=reasoner_calls, request=request,
        shared_family_plan=family_plan, note_rows=note_rows,
    )
    return {"family_plan": family_plan, "relationships": relationships}
