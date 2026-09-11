"""Private comparison transport; production readers retain their existing budgets."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Mapping, Sequence

from auto_zettelkasten import readers as r

INPUT_CEILING = 750_000
LINKING_OUTPUT_ALLOWANCE = 65_536
EXPERIMENT_ID = "v030-linking-comparison-v3-helper-idle600"
LINK_CONTRACT = "relationship_candidate_selection"
PLAN_CONTRACT = "literature_family_plan"
ROUTING_CONTRACTS = {PLAN_CONTRACT, "relationship_shard_selection", "bridge_shard_selection"}


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def direct_system_prompt() -> str:
    original = r._relationship_candidate_system_prompt()
    return (
        original[:original.index("Return one JSON object")]
        + "Return a JSON object with only candidates. Each candidate contains only "
        "left_source_id, right_source_id, decision, relation_type, actor_source_id, "
        "reference_source_id, and reason. "
        + original[original.index("Use exact supplied IDs"):original.index("When required_pairs")]
        + "Consider useful relationships throughout the supplied descriptions. Respect max_inferred_pairs "
        "and excluded_pairs; never return an excluded or duplicate pair. A shorter response ends this "
        "request, not a claim of exhaustive discovery. No invented IDs, locators or evidence inventories."
    )


class ExperimentCodexReader(r.CodexReader):
    """Use verified catalog capacity and max reasoning without changing production defaults."""

    def __init__(self, model: str, *, approach: str, max_records: int,
                 capability: Mapping[str, Any], **kwargs: Any):
        if approach not in {"planner", "direct"}:
            raise ValueError("approach must be planner or direct")
        if not isinstance(max_records, int) or isinstance(max_records, bool) or max_records <= 0:
            raise ValueError("max_records must be a positive frozen integer")
        if capability.get("slug") != model:
            raise ValueError("capability must identify the requested model")
        efforts = {row.get("effort") for row in capability.get("supported_reasoning_levels", [])}
        if "max" not in efforts:
            raise ValueError("model does not advertise maximum reasoning")
        context = int(capability.get("max_context_window") or 0)
        percent = int(capability.get("effective_context_window_percent") or 0)
        if not 0 < context <= 872_000 or not 0 < percent <= 100:
            raise ValueError("invalid or unreviewed model context capability")
        for name in ("reasoning_effort", "context_window_tokens", "direct_read_fraction"):
            if name in kwargs:
                raise ValueError(f"experiment fixes {name}")
        super().__init__(model=model, reasoning_effort="max", context_window_tokens=context,
                         direct_read_fraction=percent / 100, **kwargs)
        self.approach = approach
        self.max_records = max_records
        self.catalog_capability = dict(capability)
        self._experiment_contract: str | None = None

    @property
    def input_token_ceiling(self) -> int:
        return min(INPUT_CEILING, int(self.context_window_tokens * self.direct_read_fraction)
                   - LINKING_OUTPUT_ALLOWANCE)

    @property
    def capabilities(self) -> Mapping[str, Any]:
        result = dict(super().capabilities)
        result.update(experiment_identity=EXPERIMENT_ID, approach=self.approach,
                      input_token_ceiling=self.input_token_ceiling,
                      linking_output_allowance=LINKING_OUTPUT_ALLOWANCE,
                      reasoning_effort="max", max_records=self.max_records,
                      service_output_cap_supported=False,
                      output_allowance_enforcement="reservation_and_observed_overrun")
        result["capability_identity"] = _digest(result)
        return result

    def _codex_request_schema(self, contract_id: str) -> dict[str, Any]:
        schema = super()._codex_request_schema(contract_id)
        if self.approach == "direct" and contract_id == LINK_CONTRACT:
            schema = deepcopy(schema)
            del schema["properties"]["job_outcomes"]
            schema["required"].remove("job_outcomes")
            row = schema["properties"]["candidates"]["items"]
            for field in ("bridge_job_id", "rank"):
                del row["properties"][field]
                row["required"].remove(field)
        return schema

    def _codex_execution_identity(self, contract_id: str, effort: str, version: str) -> dict[str, Any]:
        result = super()._codex_execution_identity(contract_id, effort, version)
        result.update(experiment_identity=EXPERIMENT_ID, approach=self.approach,
                      schema_hash=_digest(self._codex_request_schema(contract_id)),
                      output_reservation=self._reserved_output_tokens(contract_id, 0),
                      model_context_window=self.context_window_tokens,
                      effective_context_window_percent=int(self.direct_read_fraction * 100),
                      max_records=self.max_records, service_output_cap_supported=False,
                      output_allowance_enforcement="reservation_and_observed_overrun",
                      capability_identity=self.capabilities["capability_identity"])
        return result

    def _codex_configuration_arguments(self) -> tuple[str, ...]:
        return ("-c", f"model_context_window={self.context_window_tokens}",
                "-c", "model_providers.openai.stream_idle_timeout_ms=600000")

    def _reserved_output_tokens(self, contract_id: str, requested: int) -> int:
        if contract_id == LINK_CONTRACT:
            return LINKING_OUTPUT_ALLOWANCE
        if self.approach == "planner" and contract_id in ROUTING_CONTRACTS:
            return super()._reserved_output_tokens(contract_id, requested)
        raise r.ProviderError("source, cluster and other calls are outside this experiment")

    def estimate_request_input(self, system: str, user: str, contract_id: str) -> int:
        # Include the actual wire wrapper, strict schema, transport instruction and conservative reserve.
        return (r._estimate_tokens(r._codex_wire_prompt(system, user, contract_id))
                + r._estimate_tokens(json.dumps(self._codex_request_schema(contract_id), sort_keys=True))
                + r._estimate_tokens(r._CODEX_TRANSPORT_INSTRUCTIONS) + self.prompt_reserve_tokens)

    def request_fits(self, system: str, user: str, contract_id: str,
                     output_tokens: int | None = None) -> bool:
        output = self._reserved_output_tokens(contract_id, 0) if output_tokens is None else output_tokens
        estimate = self.estimate_request_input(system, user, contract_id)
        return (0 < output <= int(self.capabilities["supported_output_tokens"])
                and estimate <= INPUT_CEILING
                and estimate + output <= int(self.context_window_tokens * self.direct_read_fraction))

    def _prompt_fits(self, system_prompt: str, user_prompt: str, output_tokens: int,
                     *, context_fraction: float | None = None, extra_input_tokens: int = 0) -> bool:
        contract = self._experiment_contract
        if contract is None:
            if system_prompt == r._literature_family_plan_system_prompt():
                contract = PLAN_CONTRACT
            elif system_prompt in {r._relationship_candidate_system_prompt(), direct_system_prompt()}:
                contract = LINK_CONTRACT
            elif system_prompt == r._relationship_shard_system_prompt():
                contract = "relationship_shard_selection"
            elif system_prompt == r._relationship_bridge_shard_system_prompt():
                contract = "bridge_shard_selection"
            else:
                return False
        # Current experimental contracts have no dynamically supplied schema tokens.
        if extra_input_tokens:
            return False
        return self.request_fits(system_prompt, user_prompt, contract, output_tokens)

    def _literature_json_call(self, system_prompt: str, user_prompt: str, **kwargs: Any) -> Mapping[str, Any]:
        contract = kwargs.get("contract_id")
        self._reserved_output_tokens(contract, 0)
        previous = self._experiment_contract
        self._experiment_contract = contract
        try:
            kwargs["reasoning_effort"] = "max"
            return super()._literature_json_call(system_prompt, user_prompt, **kwargs)
        finally:
            self._experiment_contract = previous

    def _generate_text(self, system: str, user: str, output: int, deadline: float) -> Any:
        contract = r._OUTPUT_CONTRACT.get()
        if r._REASONING_EFFORT.get() != "max":
            raise r.ProviderError("experimental application calls require maximum reasoning")
        self._reserved_output_tokens(contract, 0)
        if not self.request_fits(system, user, contract, output):
            raise r.ProviderError("experimental complete request exceeds admitted capacity")
        if deadline > 600:
            raise r.ProviderError("experimental child deadline exceeds 600 seconds")
        self._ensure_codex_preflight()
        actual = r._codex_model_catalog(self._preflight["_environment"])[self.model]
        fields = ("slug", "max_context_window", "effective_context_window_percent", "supported_reasoning_levels")
        if any(actual.get(key) != self.catalog_capability.get(key) for key in fields):
            raise r.ProviderError("model capability changed after experiment preparation")
        raw = super()._generate_text(system, user, output, deadline)
        completion = dict(raw.completion)
        completion["estimated_complete_input_tokens"] = self.estimate_request_input(system, user, contract)
        completion["configuration_arguments"] = list(self._codex_configuration_arguments())
        result = r._ProviderText(str(raw), completion)
        usage = completion.get("usage") or {}
        total = usage.get("output_tokens")  # Codex output_tokens already includes reasoning_output_tokens.
        reasoning = usage.get("reasoning_output_tokens")
        if (not isinstance(total, int) or isinstance(total, bool) or total < 0 or total > output
                or not isinstance(reasoning, int) or isinstance(reasoning, bool)
                or not 0 <= reasoning <= total):
            failure = r.ProviderError("experimental output allowance exceeded or output usage unavailable")
            r._preserve_provider_failure(failure, result)
            raise failure
        return result

    def direct_request(self, descriptions: Sequence[Mapping[str, Any]], *,
                       excluded_pairs: Sequence[Any] = (), source_set_id: str = "") -> tuple[str, str]:
        if self.approach != "direct":
            raise ValueError("direct request requires direct approach")
        ids = [row.get("source_id") for row in descriptions]
        if not ids or any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != len(ids):
            raise ValueError("descriptions require unique exact source identities")
        return direct_system_prompt(), json.dumps({
            "source_set_id": source_set_id, "descriptions": list(descriptions),
            "excluded_pairs": list(excluded_pairs), "max_inferred_pairs": self.max_records,
        }, ensure_ascii=False, sort_keys=True)

    def select_direct_links(self, descriptions: Sequence[Mapping[str, Any]], *,
                            excluded_pairs: Sequence[Any] = (), source_set_id: str = "") -> Mapping[str, Any]:
        self._authorize_request()
        system, user = self.direct_request(descriptions, excluded_pairs=excluded_pairs,
                                           source_set_id=source_set_id)
        return self._literature_json_call(system, user, label="experimental direct linking",
                                         contract_id=LINK_CONTRACT, list_key="candidates")

    def select_direct_candidates(self, profiles: Sequence[Any], request: Any, *,
                                 context: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        if profiles:
            raise ValueError("direct descriptions must be supplied once through context")
        context = context or {}
        return self.select_direct_links(context["descriptions"],
                                        excluded_pairs=context.get("excluded_pairs", ()),
                                        source_set_id=str(getattr(request, "source_set_id", "")))

    def _outside_experiment(self, *args: Any, **kwargs: Any) -> Any:
        raise r.ProviderError("source, cluster, adjudication and gap calls are outside this experiment")

    read = read_document = read_chunk = read_source = read_source_bundle = _outside_experiment
    summarize_chunk = synthesize_document = synthesize_document_bundle = _outside_experiment
    profile_source = verify_atomic_claims = adjudicate_relationships = verify_relationships = _outside_experiment
    propose_clusters = plan_clusters = synthesize_cluster = map_debates = detect_gaps = _outside_experiment
