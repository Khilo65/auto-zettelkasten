"""Synthetic bounded community-label tests; never contact a provider."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3

import pytest

from auto_zettelkasten.community_naming import prepare_community_name, generate_community_name
from auto_zettelkasten.readers import CodexReader, DeepSeekReader, ProviderError, CODEX_OUTPUT_CONTRACTS, codex_contract_identity


class Reader:
    name, model = "synthetic", "small-pinned"
    capabilities = {"capability_identity": "test-capability"}
    calls = 0
    allowance = 20000
    response = {"title": "Mediation strategies and competing explanations"}

    def community_name_input_budget(self):
        return self.allowance

    def community_name_output_tokens(self):
        return 128

    def name_community(self, system_prompt, user_prompt):
        self.calls += 1
        assert "Members can disagree" in system_prompt
        assert len(json.loads(user_prompt)["members"]) <= 12
        return self.response


def members(count=16):
    return [{"source_id": f"source-{i:03}", "title": f"Synthetic mediation study {i}", "thesis": "Interventions vary. Conditions change outcomes. Omit this third sentence.", "representativeness": i} for i in range(count)]


def prepare(reader=None, **kwargs):
    return prepare_community_name(reader or Reader(), members(), source_provider="synthetic", source_model="small-pinned", **kwargs)


def priced(reader=None):
    return prepare(reader, input_price_per_million="1", output_price_per_million="2", pricing_identity="synthetic-rates-v1")


def approval(preview, **kwargs):
    return {"approved": True, "budget_scope": "community_naming", "request_digest": preview["request_digest"], "approval_id": "naming-only", "max_spend_usd": "1", **kwargs}


def test_packet_uses_twelve_short_diverse_members_and_is_deterministic():
    preview = prepare()
    assert preview["member_count"] == 12
    assert preview["total_members"] == 16
    assert all("third sentence" not in row["thesis"] for row in preview["packet"]["members"])
    assert preview["request_digest"] == prepare()["request_digest"]
    assert preview["reserved_cost_usd"] is None
    assert preview["paid_dispatch_enabled"] is False


def test_disabled_default_never_creates_ledger(tmp_path):
    reader = Reader()
    result = generate_community_name(reader, priced(reader), store_path=tmp_path / "naming.db")
    assert result["status"] == "deterministic"
    assert reader.calls == 0
    assert not list(tmp_path.iterdir())


def test_trim_to_verified_allowance():
    reader = Reader()
    reader.allowance = 2200
    preview = priced(reader)
    assert 0 < preview["member_count"] < 12
    assert preview["reserved_input_tokens"] <= 2200
    reader.allowance = 50
    with pytest.raises(ValueError, match="one bounded member"):
        priced(reader)


@pytest.mark.parametrize("change", [{"source_provider": "literature"}, {"source_model": "large-fallback"}])
def test_requires_exact_configured_source_worker(change):
    args = {"source_provider": "synthetic", "source_model": "small-pinned", **change}
    with pytest.raises(ValueError, match="configured source"):
        prepare_community_name(Reader(), members(), **args)


def test_unapproved_or_jev_budget_cannot_dispatch(tmp_path):
    reader, preview = Reader(), priced()
    for auth in (None, approval(preview, approved=False), approval(preview, budget_scope="jev"), approval(preview, request_digest="stale"), approval(preview, max_spend_usd="0")):
        with pytest.raises(ValueError):
            generate_community_name(reader, preview, store_path=tmp_path / "naming.db", enabled=True, approval=auth)
    assert reader.calls == 0


def test_unknown_prices_fail_closed(tmp_path):
    reader, preview = Reader(), prepare()
    with pytest.raises(ValueError, match="explicit provider pricing"):
        generate_community_name(reader, preview, store_path=tmp_path / "naming.db", enabled=True, approval=approval(preview))
    assert reader.calls == 0


def test_cached_request_never_dispatches_twice(tmp_path):
    reader, preview = Reader(), priced()
    args = {"store_path": tmp_path / "naming.db", "enabled": True, "approval": approval(preview)}
    assert generate_community_name(reader, preview, **args)["status"] == "generated"
    result = generate_community_name(reader, preview, **args)
    assert result["status"] == "cached"
    assert result["additional_cost_usd"] == "0"
    assert reader.calls == 1


def test_changed_input_or_model_invalidates_cache():
    first = prepare()
    other = members()
    other[-1]["thesis"] = "Different condition. Contradictory finding."
    assert prepare_community_name(Reader(), other, source_provider="synthetic", source_model="small-pinned")["request_digest"] != first["request_digest"]
    reader = Reader()
    reader.model = "other-pinned"
    assert prepare_community_name(reader, members(), source_provider="synthetic", source_model="other-pinned")["request_digest"] != first["request_digest"]


def test_failed_title_keeps_reservation_and_no_retry(tmp_path):
    reader = Reader()
    reader.response = {"title": "All studies agree on one theory"}
    preview = priced(reader)
    args = {"store_path": tmp_path / "naming.db", "enabled": True, "approval": approval(preview)}
    with pytest.raises(ValueError, match="consensus"):
        generate_community_name(reader, preview, **args)
    with pytest.raises(ValueError, match="no automatic retry"):
        generate_community_name(reader, preview, **args)
    with sqlite3.connect(tmp_path / "naming.db") as conn:
        assert conn.execute("SELECT status,reserved FROM naming_requests").fetchone() == ("dispatched", preview["reserved_cost_usd"])
    assert reader.calls == 1


def test_preview_mutation_and_capability_change_rejected(tmp_path):
    reader, preview = Reader(), priced()
    changed = deepcopy(preview)
    changed["identity"]["user_prompt"] = "Other input"
    with pytest.raises(ValueError, match="identity"):
        generate_community_name(reader, changed, store_path=tmp_path / "naming.db")
    reader.allowance -= 1
    with pytest.raises(ValueError, match="capability changed"):
        generate_community_name(reader, preview, store_path=tmp_path / "naming.db", enabled=True, approval=approval(preview))


def test_concurrent_admission_reserves_at_most_once(tmp_path):
    reader, preview = Reader(), priced()
    args = {"store_path": tmp_path / "naming.db", "enabled": True, "approval": approval(preview)}
    def run(_):
        try:
            return generate_community_name(reader, preview, **args)["status"]
        except ValueError:
            return "blocked"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert results.count("generated") == 1
    assert reader.calls == 1


def test_adapters_prepare_without_keys_or_transport(monkeypatch):
    for reader in (DeepSeekReader(), CodexReader(model="gpt-6-luna")):
        def forbidden(*args, **kwargs):
            raise AssertionError("provider transport during preview")
        monkeypatch.setattr(type(reader), "_generate_text", forbidden)
        result = prepare_community_name(reader, members(), source_provider=reader.name, source_model=reader.model)
        assert result["identity"]["model"] == reader.model
        assert result["identity"]["max_output_tokens"] == 128
    assert CODEX_OUTPUT_CONTRACTS["community_name"]["properties"]["title"]["type"] == "string"
    assert codex_contract_identity("community_name", "gpt-6-luna", "medium")["output_reservation"] == 128


def test_adapter_rejects_unknown_model_context():
    reader = DeepSeekReader(model="unverified-model")
    with pytest.raises(ProviderError, match="verified model"):
        prepare_community_name(reader, members(), source_provider=reader.name, source_model=reader.model)


def test_rate_changes_reuse_exact_model_input_prompt_cache(tmp_path):
    reader = Reader()
    first = priced(reader)
    second = prepare(reader, input_price_per_million="0.5", output_price_per_million="1", pricing_identity="synthetic-rates-v2")
    assert first["request_digest"] != second["request_digest"]
    assert first["cache_digest"] == second["cache_digest"]
    store = tmp_path / "names.db"
    generate_community_name(reader, first, store_path=store, enabled=True, approval=approval(first))
    cached = generate_community_name(reader, second, store_path=store, enabled=True, approval=approval(second))
    assert cached["status"] == "cached"
    assert cached["produced_request_digest"] == first["request_digest"]
    assert reader.calls == 1


def test_distinct_requests_share_aggregate_admission_without_budget_extension(tmp_path):
    reader = Reader()
    first = priced(reader)
    other = members()
    other[-1]["thesis"] = "Changed mediator incentives. New conditions."
    second = prepare_community_name(reader, other, source_provider=reader.name, source_model=reader.model, input_price_per_million="1", output_price_per_million="2", pricing_identity="synthetic-rates-v1")
    from decimal import Decimal
    ceiling = str(max(Decimal(first["reserved_cost_usd"]), Decimal(second["reserved_cost_usd"])))
    def run(preview):
        try:
            return generate_community_name(reader, preview, store_path=tmp_path / "names.db", enabled=True, approval=approval(preview, max_spend_usd=ceiling))["status"]
        except ValueError:
            return "blocked"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, (first, second)))
    assert sorted(results) == ["blocked", "generated"]
    assert reader.calls == 1
    with pytest.raises(ValueError, match="budget exhausted or changed"):
        generate_community_name(reader, second if results[0] == "generated" else first, store_path=tmp_path / "names.db", enabled=True, approval=approval(second if results[0] == "generated" else first, max_spend_usd="2"))


@pytest.mark.parametrize("bad", [True, "NaN", "Infinity", "-1"])
def test_invalid_pricing_never_admitted(bad):
    with pytest.raises(ValueError, match="nonnegative decimal"):
        prepare(input_price_per_million=bad, output_price_per_million="1", pricing_identity="synthetic")


def test_deterministic_label_is_pure_bounded_and_validated():
    from auto_zettelkasten.community_naming import deterministic_community_label
    rows = members()
    original = deepcopy(rows)
    assert deterministic_community_label(rows) == prepare()["fallback_title"]
    assert deterministic_community_label(list(reversed(rows))) == deterministic_community_label(rows)
    assert deterministic_community_label(rows, ["mediator incentives", "institutions", "competing explanations"]) == "mediator incentives, institutions, competing explanations"
    assert len(deterministic_community_label(rows, ["x" * 100] * 3)) == 100
    assert rows == original
    with pytest.raises(ValueError, match="unique"):
        deterministic_community_label(rows + [rows[0]])
    with pytest.raises(ValueError, match="bounded sequence"):
        deterministic_community_label(rows, "invalid string")
