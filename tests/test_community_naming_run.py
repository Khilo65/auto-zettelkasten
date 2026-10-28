"""Hermetic staged naming execution, reuse and aggregate spending boundaries."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import sqlite3
import threading

import pytest

from auto_zettelkasten.community_naming_pipeline import CommunityNamingCache, prepare_two_stage_naming
from auto_zettelkasten.community_naming_run import run_two_stage_naming


def prepared(*, changed=False, count=2):
    corpus = []
    groups = []
    for at in range(count):
        text = f"Topic {at} concerns institutional conditions." + (" Changed evidence." if changed and at == 0 else "")
        corpus.append({"source_id": f"s{at}", "title": f"Institutional topic {at}", "text": text,
                       "content_sha256": hashlib.sha256(text.encode()).hexdigest()})
        groups.append({"community_id": f"g{at}", "source_ids": [f"s{at}"]})
    return prepare_two_stage_naming(groups, corpus)


class Reader:
    name, model, is_cloud = "openai", "gpt-6-luna", True

    def __init__(self):
        self.calls = []
        self.error = None

    def name_community_stage(self, system, user, *, stage, output_tokens):
        self.calls.append(stage)
        if self.error:
            raise self.error
        packet = json.loads(user)
        if stage == "local":
            cid = packet["community_id"]
            return {"labels": [{"community_id": cid, "title": f"Institutional topic {cid}",
                                "scope": "Institutional conditions and their interpretation."}]}
        return {"labels": [{"community_id": g["community_id"], "title": g["title"]}
                            for g in packet["communities"]]}


RATES = {"input_price_per_million": ".1", "output_price_per_million": ".5",
         "pricing_identity": "synthetic-unit-rates-v1"}


def preview(cache, value):
    return run_two_stage_naming(value, cache, **RATES)


def auth(plan, **updates):
    return {"approved": True, "budget_scope": "community_naming", "approval_id": "test-naming-budget",
            "request_digest": plan["request_digest"], "max_spend_usd": "1", **updates}


def execute(cache, value, reader, *, approval=None, ledger_path=None):
    plan = preview(cache, value)
    return run_two_stage_naming(value, cache, reader=reader, enabled=True,
                                approval=approval or auth(plan), ledger_path=ledger_path, **RATES)


def test_inert_default_has_no_reader_calls_or_spending_ledger(tmp_path):
    reader = Reader()
    reader.error = AssertionError("provider during preview")
    cache = CommunityNamingCache(tmp_path / "cache.db")
    result = run_two_stage_naming(prepared(), cache, reader=reader)
    assert result["status"] == "preview"
    assert result["provider_calls"] == 0
    assert result["missing_request_count"] == 3
    assert result["reserved_cost_usd"] is None
    assert result["paid_dispatch_enabled"] is False
    assert not reader.calls
    assert not list(tmp_path.glob("*spending*"))


def test_generate_three_calls_then_reuse_with_no_provider_or_prices(tmp_path):
    cache, reader = CommunityNamingCache(tmp_path / "cache.db"), Reader()
    value = prepared(); before = deepcopy(value)
    result = execute(cache, value, reader)
    assert result["provider_calls"] == 3
    assert reader.calls == ["local", "local", "reconciliation"]
    assert result["status"] == "generated"
    assert result["actual_cost_usd"] is None
    assert result["exact_request_cache_hits"] == 3
    assert result["final"]["g0"]["title"] == "Institutional topic g0"
    assert value == before
    replay = run_two_stage_naming(value, cache, enabled=True)
    assert replay["status"] == "cached"
    assert replay["provider_calls"] == 0
    assert replay["additional_cost_usd"] == "0"
    assert len(reader.calls) == 3


def test_changed_one_community_reuses_other_draft_and_reconciles(tmp_path):
    cache, reader = CommunityNamingCache(tmp_path / "cache.db"), Reader()
    execute(cache, prepared(), reader)
    changed = prepared(changed=True)
    plan = preview(cache, changed)
    assert list(plan["missing_local_jobs"]) == ["g0"]
    assert plan["missing_request_count"] == 2
    result = execute(cache, changed, reader)
    assert result["provider_calls"] == 2
    assert len(reader.calls) == 5


@pytest.mark.parametrize("change", [
    {"approved": False}, {"budget_scope": "jev"}, {"request_digest": "stale"},
    {"max_spend_usd": "0"}, {"approval_id": ""},
])
def test_wrong_approval_never_creates_spending_ledger(tmp_path, change):
    cache, reader = CommunityNamingCache(tmp_path / "cache.db"), Reader()
    value = prepared(); plan = preview(cache, value)
    with pytest.raises(ValueError):
        execute(cache, value, reader, approval=auth(plan, **change))
    assert not reader.calls
    assert not list(tmp_path.glob("*spending*"))


def test_missing_rates_or_wrong_reader_fails_closed(tmp_path):
    cache, reader = CommunityNamingCache(tmp_path / "cache.db"), Reader()
    value = prepared()
    with pytest.raises(ValueError, match="pricing"):
        run_two_stage_naming(value, cache, enabled=True, reader=reader)
    reader.model = "different-model"
    with pytest.raises(ValueError, match="pinned"):
        execute(cache, value, reader)
    assert not reader.calls


@pytest.mark.parametrize("bad", [True, "NaN", "Infinity", "-1"])
def test_invalid_pricing_rejected(tmp_path, bad):
    cache = CommunityNamingCache(tmp_path / "cache.db")
    with pytest.raises(ValueError):
        run_two_stage_naming(prepared(), cache, input_price_per_million=bad,
                             output_price_per_million=".5", pricing_identity="synthetic")


def test_unknown_keeps_reservation_and_cannot_retry_with_another_budget(tmp_path):
    cache, reader = CommunityNamingCache(tmp_path / "cache.db"), Reader()
    reader.error = RuntimeError("synthetic transport timeout")
    value = prepared()
    with pytest.raises(RuntimeError):
        execute(cache, value, reader)
    assert len(reader.calls) == 1
    ledger = tmp_path / "cache.db.spending.sqlite3"
    with sqlite3.connect(ledger) as db:
        rows = db.execute("SELECT reserved,status,result FROM naming_calls").fetchall()
    assert len(rows) == 1 and Decimal(rows[0][0]) > 0 and rows[0][1:] == ("unknown", None)
    reader.error = None
    for identity in ("test-naming-budget", "new-budget"):
        with pytest.raises(ValueError, match="unknown"):
            execute(cache, value, reader, approval=auth(preview(cache, value), approval_id=identity))
    assert len(reader.calls) == 1


def test_bad_endpoint_output_holds_and_does_not_enter_cache(tmp_path):
    class BadReader(Reader):
        def name_community_stage(self, *args, **kwargs):
            self.calls.append(kwargs["stage"])
            return {"labels": [{"community_id": "unrequested", "title": "Invented", "scope": "Unsupported."}]}
    cache, reader = CommunityNamingCache(tmp_path / "cache.db"), BadReader()
    with pytest.raises(ValueError, match="identity"):
        execute(cache, prepared(), reader)
    assert cache.replay(prepared())["drafts"] == {}
    assert len(reader.calls) == 1


def test_ledger_recovers_complete_response_without_call_if_result_cache_missing(tmp_path):
    first, reader = CommunityNamingCache(tmp_path / "first.db"), Reader()
    ledger = tmp_path / "spending.db"
    execute(first, prepared(), reader, ledger_path=ledger)
    second = CommunityNamingCache(tmp_path / "second.db")
    reader.error = AssertionError("repeat provider call")
    replay = execute(second, prepared(), reader, ledger_path=ledger)
    assert replay["status"] == "cached" and replay["provider_calls"] == 0
    assert replay["final"] == first.replay(prepared())["final"]
    assert len(reader.calls) == 3


def test_concurrent_admission_does_not_dispatch_past_an_unknown_request(tmp_path):
    cache = CommunityNamingCache(tmp_path / "cache.db")
    entered, release = threading.Event(), threading.Event()
    class BlockingReader(Reader):
        def name_community_stage(self, *args, **kwargs):
            if not self.calls:
                entered.set()
                assert release.wait(3)
            return super().name_community_stage(*args, **kwargs)
    reader = BlockingReader(); value = prepared(count=1); other = prepared(changed=True, count=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(execute, cache, value, reader)
        assert entered.wait(3)
        blocked = pool.submit(execute, cache, other, Reader())
        try:
            with pytest.raises(ValueError, match="unknown"):
                blocked.result(timeout=3)
        finally:
            release.set()
        assert pending.result(timeout=3)["provider_calls"] == 2


def test_budget_cannot_be_increased_for_an_existing_approval(tmp_path):
    cache, reader = CommunityNamingCache(tmp_path / "cache.db"), Reader()
    execute(cache, prepared(count=1), reader)
    other = prepared(changed=True, count=1)
    with pytest.raises(ValueError, match="ceiling cannot be changed"):
        execute(cache, other, reader, approval=auth(preview(cache, other), max_spend_usd="2"))
    assert len(reader.calls) == 2


def test_ledger_symlink_never_touches_target(tmp_path):
    target = tmp_path / "keep"
    target.write_text("untouched")
    link = tmp_path / "ledger"
    link.symlink_to(target)
    cache, reader = CommunityNamingCache(tmp_path / "cache.db"), Reader()
    with pytest.raises(ValueError, match="symbolic"):
        execute(cache, prepared(), reader, ledger_path=link)
    assert target.read_text() == "untouched" and not reader.calls


def test_mutated_display_bounds_cannot_change_admission(tmp_path):
    value = prepared()
    value["local_jobs"]["g0"]["input_token_upper_bound"] = 1
    cache = CommunityNamingCache(tmp_path / "cache.db")
    with pytest.raises(ValueError, match="bound"):
        preview(cache, value)


def test_reconciliation_prompt_change_requires_new_exact_approval(tmp_path, monkeypatch):
    import auto_zettelkasten.community_naming_pipeline as core
    cache, reader = CommunityNamingCache(tmp_path / "cache.db"), Reader()
    original = prepared()
    old_plan = preview(cache, original)
    monkeypatch.setattr(core, "RECONCILIATION_PROMPT", core.RECONCILIATION_PROMPT + " New exact wording.")
    changed = prepared()
    new_plan = preview(cache, changed)
    assert new_plan["request_digest"] != old_plan["request_digest"]
    with pytest.raises(ValueError, match="exact"):
        execute(cache, changed, reader, approval=auth(old_plan))
    assert not reader.calls
