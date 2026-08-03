from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from auto_zettelkasten.files import read_yaml, write_yaml
from auto_zettelkasten.api import resume_map
from auto_zettelkasten.models import (
    ArtifactManifest,
    MapRequest,
    ProcessingPolicy,
    RunReport,
)
from auto_zettelkasten.workspace import run_directory
from auto_zettelkasten.pipeline import (
    _LocalAcquisitionGate,
    _ProfileProviderBudget,
    _RunProgress,
    _read_document,
    _provider_call_with_transport_retry,
    _source_worker_count,
    _write_source_replay_receipt,
)
from auto_zettelkasten.readers import ProviderError, SECTION_KEYS


class _Reader:
    def __init__(self, *, name: str, model: str, is_cloud: bool) -> None:
        self.name = name
        self.model = model
        self.is_cloud = is_cloud


def test_source_worker_resolution_separates_local_and_provider_limits(tmp_path) -> None:
    request = MapRequest(
        tmp_path,
        parallel=4,
        provider_concurrency="auto",
    )
    deepseek = _Reader(
        name="deepseek", model="deepseek-v4-flash", is_cloud=True
    )
    other_cloud = _Reader(name="deepseek", model="another-model", is_cloud=True)
    local = _Reader(name="ollama", model="local", is_cloud=False)

    assert _source_worker_count(deepseek, request, 500) == 256
    assert _source_worker_count(other_cloud, request, 500) == 32
    assert _source_worker_count(local, request, 500) == 4
    assert (
        _source_worker_count(
            deepseek,
            MapRequest(tmp_path, parallel=4, provider_concurrency=73),
            500,
        )
        == 73
    )


def test_500_item_provider_stress_keeps_local_work_at_four_and_scales() -> None:
    def run(workers: int) -> tuple[float, int, int, list[tuple[int, str]]]:
        local_gate = _LocalAcquisitionGate(4)
        provider_lock = threading.Lock()
        provider_active = 0
        provider_peak = 0

        def process(index: int) -> tuple[int, str]:
            nonlocal provider_active, provider_peak
            with local_gate:
                time.sleep(0.0002)
            with provider_lock:
                provider_active += 1
                provider_peak = max(provider_peak, provider_active)
            try:
                time.sleep(0.02)
                return index, f"source-{index}"
            finally:
                with provider_lock:
                    provider_active -= 1

        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(process, range(500)))
        return time.monotonic() - started, local_gate.peak, provider_peak, results

    slow_time, slow_local, slow_provider, slow_results = run(32)
    fast_time, fast_local, fast_provider, fast_results = run(256)

    assert slow_local <= 4
    assert fast_local <= 4
    assert slow_provider <= 32
    assert 32 < fast_provider <= 256
    assert slow_results == fast_results
    assert len({source_id for _index, source_id in fast_results}) == 500
    assert fast_time < slow_time * 0.75


def test_provider_event_reservation_counts_after_interruption(tmp_path) -> None:
    path = tmp_path / "provider_usage.yml"
    first = _ProfileProviderBudget(path, 1)
    attempt_id = first.reserve("source_bundle_direct", "A", "hash-a")

    resumed = _ProfileProviderBudget(path, 10)

    assert resumed.max_calls == 1
    assert resumed.cumulative_calls == 1
    assert resumed.attempts[0]["attempt_id"] == attempt_id
    assert resumed.attempts[0]["status"] == "started"
    with pytest.raises(RuntimeError, match="source_profile_call_budget_reached"):
        resumed.reserve("source_bundle_direct", "B", "hash-b")


def test_transport_failure_retries_once_and_counts_both_attempts(tmp_path) -> None:
    budget = _ProfileProviderBudget(tmp_path / "provider_usage.yml", 2)
    calls = 0

    def operation() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ProviderError("provider request timed out")
        return "ok"

    assert (
        _provider_call_with_transport_retry(
            budget,
            "source_bundle_direct",
            "A",
            "hash-a",
            operation,
        )
        == "ok"
    )
    assert calls == 2
    assert budget.cumulative_calls == 2
    assert [row["status"] for row in budget.attempts] == ["failed", "completed"]


def test_provider_event_ledger_is_exact_under_concurrent_calls(tmp_path) -> None:
    path = tmp_path / "provider_usage.yml"
    budget = _ProfileProviderBudget(path, 100)

    def call(index: int) -> str:
        attempt_id = budget.reserve("source_bundle_direct", str(index), "same")
        budget.finish(
            attempt_id,
            status="completed",
            provider_completion={"response_id": f"response-{index}"},
        )
        return attempt_id

    with ThreadPoolExecutor(max_workers=64) as executor:
        attempt_ids = list(executor.map(call, range(100)))
    budget.flush()
    resumed = _ProfileProviderBudget(path, 100)

    assert len(set(attempt_ids)) == 100
    assert resumed.cumulative_calls == 100
    assert all(row["status"] == "completed" for row in resumed.attempts)
    assert {
        row["provider_completion"]["response_id"] for row in resumed.attempts
    } == {f"response-{index}" for index in range(100)}
    assert read_yaml(path)["provider_call_count"] == 100


def test_hierarchical_calls_are_sequential_per_document_but_overlap_across_documents(
    tmp_path,
) -> None:
    class HierarchicalReader:
        name = "fake-cloud"
        model = "fake"
        is_cloud = True
        context_window_tokens = 0

        def __init__(self) -> None:
            self.lock = threading.Lock()
            self.active_by_source: dict[str, int] = {}
            self.peak_by_source: dict[str, int] = {}
            self.global_active = 0
            self.global_peak = 0

        def _call(self, metadata):
            key = metadata["_source_context"]["zotero_key"]
            with self.lock:
                self.active_by_source[key] = self.active_by_source.get(key, 0) + 1
                self.peak_by_source[key] = max(
                    self.peak_by_source.get(key, 0), self.active_by_source[key]
                )
                self.global_active += 1
                self.global_peak = max(self.global_peak, self.global_active)
            time.sleep(0.01)
            with self.lock:
                self.active_by_source[key] -= 1
                self.global_active -= 1

        def summarize_chunk(self, chunk, metadata, question, **_kwargs):
            del chunk, question
            self._call(metadata)
            return {"summary": "chunk"}

        def synthesize_document(self, analyses, metadata, question, **_kwargs):
            del analyses, question
            self._call(metadata)
            return {key: f"value for {key}" for key in SECTION_KEYS}

    reader = HierarchicalReader()
    request = MapRequest(
        tmp_path,
        processing=ProcessingPolicy(
            direct_read_char_limit=10,
            chunk_char_limit=15,
            max_total_chunks=64,
            max_calls_per_document_run=24,
        ),
    )
    budget = _ProfileProviderBudget(tmp_path / "provider_usage.yml", 100)

    def read(index: int):
        key = f"SOURCE-{index}"
        return _read_document(
            reader,
            "one two three\n\nfour five six\n\nseven eight nine",
            {
                "_source_context": {
                    "source_id": f"source-{index}",
                    "zotero_key": key,
                }
            },
            None,
            request=request,
            provider_budget=budget,
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(read, range(4)))

    assert len(results) == 4
    assert reader.global_peak > 1
    assert set(reader.peak_by_source.values()) == {1}


def test_legacy_provider_usage_migration_is_idempotent(tmp_path) -> None:
    path = tmp_path / "provider_usage.yml"
    write_yaml(
        path,
        {
            "usage_schema_version": "2",
            "max_calls": 2,
            "attempts": [
                {
                    "attempt_id": "legacy-attempt",
                    "stage": "source_bundle_direct",
                    "key": "A",
                    "fingerprint": "hash-a",
                    "attempt": 1,
                    "status": "completed",
                    "started_at": "2026-08-02T00:00:00+00:00",
                    "finished_at": "2026-08-02T00:00:01+00:00",
                }
            ],
        },
    )

    first = _ProfileProviderBudget(path, 10)
    event_text = first.events_path.read_text(encoding="utf-8")
    second = _ProfileProviderBudget(path, 10)

    assert first.max_calls == second.max_calls == 2
    assert second.cumulative_calls == 1
    assert second.events_path.read_text(encoding="utf-8") == event_text


def test_progress_updates_are_throttled_and_barriers_force_writes(
    tmp_path, monkeypatch
) -> None:
    import auto_zettelkasten.pipeline as pipeline

    writes = 0
    real_write_yaml = pipeline.write_yaml

    def counted_write(path, value):
        nonlocal writes
        writes += 1
        real_write_yaml(path, value)

    monkeypatch.setattr(pipeline, "_PROGRESS_WRITE_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(pipeline, "write_yaml", counted_write)
    path = tmp_path / "progress.yml"
    progress = _RunProgress(path, "run", [{"key": "A"}], resume=False)

    for index in range(50):
        progress.update(0, status="active", phase=f"phase-{index}")
    assert writes == 1
    time.sleep(0.08)
    assert writes == 2

    progress.update(0, status="validated_note")
    progress.set_stage("source_terminal_barrier")
    assert writes == 3
    assert read_yaml(path)["stage"] == "source_terminal_barrier"
    assert read_yaml(path)["validated_note_count"] == 1


def test_completed_resume_is_zero_call_and_zero_write(tmp_path, monkeypatch) -> None:
    run_id = "completed-run"
    run_root = run_directory(tmp_path, run_id)
    request_path = run_root / "request.yml"
    report_path = run_root / "run_report.yml"
    write_yaml(request_path, MapRequest(tmp_path).to_dict())
    report = RunReport(
        status="completed_with_parked_items",
        workspace=tmp_path,
        run_id=run_id,
        parked_for_review_count=1,
        artifact_manifest=ArtifactManifest(status="built", workspace=tmp_path),
    )
    write_yaml(report_path, report.to_dict())
    _write_source_replay_receipt(
        tmp_path,
        run_root,
        MapRequest(tmp_path),
        report,
    )
    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (request_path, report_path)
    }

    def unexpected_run(*_args, **_kwargs):
        raise AssertionError("completed replay must not enter the pipeline")

    monkeypatch.setattr("auto_zettelkasten.api.run_map", unexpected_run)
    replay = resume_map(tmp_path, run_id)

    assert replay.status == "completed_with_parked_items"
    assert isinstance(replay.artifact_manifest, ArtifactManifest)
    assert {
        path: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in (request_path, report_path)
    } == before
