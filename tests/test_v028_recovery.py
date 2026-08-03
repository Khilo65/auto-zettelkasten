from __future__ import annotations

import sys
import threading
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_zettelkasten.api import run_map
from auto_zettelkasten.extraction import ExtractionCancelled, _run_cancellable
from auto_zettelkasten.files import read_yaml, write_yaml
from auto_zettelkasten.models import LiteratureMappingPolicy, MapRequest, ProcessingPolicy
from auto_zettelkasten.pipeline import (
    ProviderCallLimitReached,
    ProviderSpendLimitReached,
    _ProfileProviderBudget,
    _RunProgress,
    _recover_finalized_prepared_result,
    _source_worker_count,
)
from auto_zettelkasten.readers import ProviderTransportError

from conftest import FakeZotero


class _DeepSeekStub:
    name = "deepseek"
    model = "deepseek-v4-flash"
    is_cloud = True


def _items(count: int) -> list[dict[str, object]]:
    return [
        {
            "key": f"ITEM{index:04d}",
            "data": {
                "key": f"ITEM{index:04d}",
                "itemType": "journalArticle",
                "title": f"Study {index}",
                "date": "2026",
                "creators": [
                    {
                        "creatorType": "author",
                        "firstName": "Test",
                        "lastName": f"Author{index}",
                    }
                ],
            },
        }
        for index in range(count)
    ]


def test_v028_defaults_remove_total_work_ceilings(tmp_path: Path) -> None:
    request = MapRequest(tmp_path)

    assert request.processing == ProcessingPolicy()
    assert request.processing.max_total_chunks == 0
    assert request.processing.max_calls_per_document_run == 0
    assert request.processing.document_deadline_seconds == 0
    assert request.literature_policy == LiteratureMappingPolicy()
    assert request.literature_policy.max_profile_calls == 0
    assert request.literature_policy.max_synthesis_calls == 0
    assert request.literature_policy.max_memberships == 0
    assert request.literature_policy.literature_deadline_seconds == 0


def test_explicit_provider_concurrency_is_not_clamped_to_auto_default(
    tmp_path: Path,
) -> None:
    reader = _DeepSeekStub()

    assert _source_worker_count(
        reader,
        MapRequest(tmp_path, provider_concurrency=512),
        1_000,
    ) == 512
    assert _source_worker_count(
        reader,
        MapRequest(tmp_path, provider_concurrency=2_500),
        3_000,
    ) == 2_500
    with pytest.raises(ValueError, match="account limit 2500"):
        MapRequest(tmp_path, provider_concurrency=2_501)


def test_dollar_budget_uses_provider_usage_and_drains_before_next_attempt(
    tmp_path: Path,
) -> None:
    budget = _ProfileProviderBudget(
        tmp_path / "provider_usage.yml",
        0,
        max_spend_usd=Decimal("0.000001"),
    )
    attempt_id = budget.reserve("source", "A", "fingerprint")
    budget.finish(
        attempt_id,
        status="completed",
        provider_completion={
            "usage": {
                "prompt_cache_miss_tokens": 0,
                "prompt_cache_hit_tokens": 0,
                "completion_tokens": 10,
            }
        },
    )

    assert budget.cumulative_spend_usd == Decimal("0.0000028")
    with pytest.raises(ProviderSpendLimitReached):
        budget.reserve("source", "B", "fingerprint")


def test_explicit_call_limit_uses_controlled_pause_exception(tmp_path: Path) -> None:
    budget = _ProfileProviderBudget(tmp_path / "provider_usage.yml", 1)
    budget.reserve("source", "A", "fingerprint")

    with pytest.raises(ProviderCallLimitReached):
        budget.reserve("source", "B", "fingerprint")


def test_dollar_budget_stops_when_provider_usage_is_unknown(tmp_path: Path) -> None:
    budget = _ProfileProviderBudget(
        tmp_path / "provider_usage.yml",
        0,
        max_spend_usd=Decimal("1"),
    )
    attempt_id = budget.reserve("source", "A", "fingerprint")
    budget.finish(attempt_id, status="failed", provider_completion={})

    assert budget.metrics()["provider_spend_unknown"] is True
    with pytest.raises(ProviderSpendLimitReached, match="provider_spend_unknown"):
        budget.reserve("source", "B", "fingerprint")


def test_cumulative_provider_calls_survive_ledger_resume(tmp_path: Path) -> None:
    path = tmp_path / "provider_usage.yml"
    first = _ProfileProviderBudget(path, 0)
    attempt_id = first.reserve("source", "A", "fingerprint")
    first.finish(
        attempt_id,
        status="completed",
        provider_completion={"usage": {"completion_tokens": 1}},
    )
    first.flush()

    resumed = _ProfileProviderBudget(path, 0)

    assert resumed.cumulative_calls == 1
    assert resumed.new_calls == 0


def test_paused_transport_remains_visible_as_pending_partial(tmp_path: Path) -> None:
    item = _items(1)[0]
    item["terminal_status"] = "paused_transport"
    progress_path = tmp_path / "progress.yml"
    progress = _RunProgress(progress_path, "run", [item], resume=False)
    progress.flush()
    payload = read_yaml(progress_path, {})

    assert payload["paused_transport_count"] == 1
    assert payload["partial_count"] == 1
    assert payload["pending_count"] == 1
    assert payload["terminal_count"] == 0


def test_active_local_subprocess_is_cancelled_promptly() -> None:
    cancelled = threading.Event()
    timer = threading.Timer(0.1, cancelled.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(ExtractionCancelled):
            _run_cancellable(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                text=True,
                timeout=60,
                cancelled=cancelled.is_set,
            )
    finally:
        timer.cancel()

    assert time.monotonic() - started < 3


def test_post_commit_fingerprint_recovers_prepared_receipt_without_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import auto_zettelkasten.pipeline as pipeline

    prepared_path = tmp_path / "prepared.yml"
    note_path = tmp_path / "02_source_memory" / "notes" / "note.md"
    note_path.parent.mkdir(parents=True)
    note_path.write_text("committed", encoding="utf-8")
    row = {
        "_prepared_status": "ready",
        "fingerprint": "fingerprint",
        "source_id": "source-A",
        "zotero_item_key": "A",
    }
    write_yaml(
        prepared_path,
        {
            "prepared_result_version": "2",
            "status": "ready",
            "row": {
                key: value
                for key, value in row.items()
                if key != "_prepared_status"
            },
        },
    )
    write_yaml(
        tmp_path / "11_state" / "fingerprints" / "fingerprint.yml",
        {
            "source_id": "source-A",
            "note_path": str(note_path.relative_to(tmp_path)),
        },
    )
    monkeypatch.setattr(
        pipeline,
        "read_note",
        lambda _path: {
            "frontmatter": {"note_status": "metadata_only_source_note"}
        },
    )
    monkeypatch.setattr(pipeline, "internal_note_text", lambda _path: "note")
    monkeypatch.setattr(
        pipeline, "validate_note", lambda _text: SimpleNamespace(passed=True)
    )

    recovered = _recover_finalized_prepared_result(
        tmp_path, prepared_path, row
    )

    assert recovered["_prepared_status"] == "committed"
    assert recovered["terminal_status"] == "limited_note"
    assert read_yaml(prepared_path, {})["status"] == "committed"


def test_cancelled_provider_call_does_not_start_transport_retry(
    tmp_path: Path,
) -> None:
    from auto_zettelkasten.pipeline import _provider_call_with_transport_retry

    budget = _ProfileProviderBudget(tmp_path / "provider_usage.yml", 0)
    cancelled = threading.Event()
    calls = 0

    def operation() -> None:
        nonlocal calls
        calls += 1
        cancelled.set()
        raise ProviderTransportError(
            "response closed during shutdown",
            transport_kind="interrupted_stream",
        )

    with pytest.raises(ProviderTransportError):
        _provider_call_with_transport_retry(
            budget,
            "source_bundle_direct",
            "A",
            "fingerprint",
            operation,
            cancelled=cancelled.is_set,
        )

    assert calls == 1
    assert budget.cumulative_calls == 1


def test_active_response_cancellation_interrupts_socket_without_buffered_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import auto_zettelkasten.readers as readers

    class FakeSocket:
        shutdown_calls: list[int] = []

        def shutdown(self, how: int) -> None:
            self.shutdown_calls.append(how)

    class FakeResponse:
        def __init__(self) -> None:
            self.fp = SimpleNamespace(raw=SimpleNamespace(_sock=FakeSocket()))
            self.closed = False

        def close(self) -> None:
            self.closed = True

    response = FakeResponse()
    monkeypatch.setattr(readers, "_ACTIVE_RESPONSES", {1: response})

    assert readers.cancel_active_provider_responses() == 1
    assert response.fp.raw._sock.shutdown_calls
    assert response.closed is False


def test_frozen_sources_fill_provider_pool_while_local_workers_are_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import auto_zettelkasten.pipeline as pipeline

    run_id = "dual-executor"
    items = _items(68)
    local_keys = {f"ITEM{index:04d}" for index in range(4)}
    run_root = tmp_path / "11_state" / "runs" / run_id
    for item in items[4:]:
        key = str(item["key"])
        root = run_root / "items" / key
        root.mkdir(parents=True, exist_ok=True)
        (root / "source.txt").write_text("frozen text", encoding="utf-8")
        write_yaml(root / "frozen_content.yml", {"content_hash": key})

    release_local = threading.Event()
    lock = threading.Lock()
    local_active = 0
    local_peak = 0
    provider_active = 0
    provider_peak = 0
    provider_started_while_local_blocked = 0
    provider_completed = 0
    first_commit_provider_completed = 0

    def acquire(
        _workspace, _run_dir, _index, item, *_args, **_kwargs
    ):
        nonlocal local_active, local_peak
        key = str(item["key"])
        assert key in local_keys
        with lock:
            local_active += 1
            local_peak = max(local_peak, local_active)
        release_local.wait(5)
        with lock:
            local_active -= 1
        return None

    def prepare(
        _workspace,
        _run_dir,
        index,
        item,
        *_args,
        **_kwargs,
    ):
        nonlocal provider_active, provider_peak
        nonlocal provider_started_while_local_blocked, provider_completed
        with lock:
            provider_active += 1
            provider_peak = max(provider_peak, provider_active)
            if local_active == 4 and not release_local.is_set():
                provider_started_while_local_blocked += 1
                if provider_started_while_local_blocked >= 32:
                    release_local.set()
        time.sleep(0.01)
        with lock:
            provider_active -= 1
            provider_completed += 1
        key = str(item["key"])
        return {
            "inventory_index": index,
            "item": dict(item),
            "zotero_item_key": key,
            "source_id": f"source-{key}",
            "note_id": "",
            "note_path": "",
            "terminal_status": "limited_note",
            "note_status": "metadata_only_source_note",
            "reason": "test",
            "attempts": [],
        }

    def finalize(_workspace, _request, _controller, row, *_args, **_kwargs):
        nonlocal first_commit_provider_completed
        with lock:
            if first_commit_provider_completed == 0:
                first_commit_provider_completed = provider_completed
        time.sleep(0.01)
        return pipeline._public_terminal_row(row), None, [], []

    monkeypatch.setattr(pipeline, "_acquire_and_freeze_item", acquire)
    monkeypatch.setattr(pipeline, "_prepare_item", prepare)
    monkeypatch.setattr(pipeline, "_finalize_prepared_row", finalize)

    report = run_map(
        MapRequest(
            tmp_path,
            provider="deepseek",
            model="deepseek-v4-flash",
            allow_cloud=True,
            parallel=4,
            provider_concurrency=64,
            literature_policy=LiteratureMappingPolicy(synthesis_enabled=False),
        ),
        client=FakeZotero(items),
        reader=_DeepSeekStub(),
        run_id=run_id,
    )

    assert report.pending_count == 0
    assert local_peak == 4
    assert provider_started_while_local_blocked >= 32
    assert provider_peak > 4
    assert first_commit_provider_completed > 1
