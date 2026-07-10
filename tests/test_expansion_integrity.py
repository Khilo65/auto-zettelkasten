from __future__ import annotations

import multiprocessing
from pathlib import Path
from typing import Any, Mapping

import pytest

from auto_zettelkasten.api import (
    decide_expansion,
    doctor,
    export_expansion_candidates,
    get_status,
    initialize_workspace,
    list_expansion_candidates,
)
from auto_zettelkasten.citations import backfill_citation_sidecars, write_citation_sidecar
from auto_zettelkasten.expansion import render_expansion_projection
from auto_zettelkasten.files import read_yaml, sha256_text, write_yaml
from auto_zettelkasten.models import ExpansionCandidate, ExpansionDecision

from conftest import FakeZotero


def _candidate() -> ExpansionCandidate:
    return ExpansionCandidate(
        work_id="work-doi-integrity",
        suggestion_id="suggestion-integrity",
        title="Integrity candidate",
        target_scope="source",
        target_id="source-seed",
        target_ids=["source-seed"],
        primary_relation="cites",
        score=0.8,
        actionability="ready",
    )


def _write_candidate(workspace: Path, candidate: ExpansionCandidate) -> None:
    write_yaml(
        workspace / "03_literature_synthesis" / "expansion" / "candidates.yml",
        {
            "engine_version": "0.2.0",
            "artifact_schema_version": "1.1",
            "candidates": [candidate.to_dict()],
        },
    )


def _decision_worker(workspace: str, decision: str, ready, results) -> None:
    ready.wait()
    try:
        row = decide_expansion(
            workspace,
            ExpansionDecision("suggestion-integrity", 0, decision, f"concurrent {decision}"),
        )
        results.put(("ok", row.state))
    except Exception as exc:  # process boundary reports exact conflict class
        results.put((type(exc).__name__, str(exc)))


def test_decision_compare_and_swap_is_process_safe(tmp_path: Path) -> None:
    if "spawn" not in multiprocessing.get_all_start_methods():
        pytest.skip("process-lock test requires multiprocessing spawn")
    initialize_workspace(tmp_path)
    _write_candidate(tmp_path, _candidate())
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    results = context.Queue()
    processes = [
        context.Process(target=_decision_worker, args=(str(tmp_path), decision, ready, results))
        for decision in ("accepted", "rejected")
    ]
    for process in processes:
        process.start()
    ready.set()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    outcomes = [results.get(timeout=2) for _ in processes]
    results.close()
    assert sum(outcome[0] == "ok" for outcome in outcomes) == 1
    assert sum(outcome[0] == "RuntimeError" and "stale" in outcome[1] for outcome in outcomes) == 1
    [current] = list_expansion_candidates(tmp_path)
    assert current.decision_version == 1
    ledger = read_yaml(tmp_path / "03_literature_synthesis" / "expansion" / "decisions.yml")["decisions"]
    assert len(ledger) == 1


def test_sidecar_metadata_refresh_never_downgrades_succeeded_references(tmp_path: Path) -> None:
    initialize_workspace(tmp_path)
    content_hash = "a" * 64
    first = {
        "key": "A",
        "data": {
            "key": "A",
            "title": "A",
            "relations": {"dc:references": "http://zotero/items/B"},
        },
    }
    path = write_citation_sidecar(
        tmp_path,
        item=first,
        source_id="source-zotero-a",
        text="References\nB Author. 2024. B. https://doi.org/10.8000/b",
        content_hash=content_hash,
        source_file="zotero://select/library/items/APDF",
        extraction_version="7",
    )
    original = read_yaml(path)
    assert original["reference_extraction_status"] == "succeeded"
    assert original["references"]

    changed = {
        "key": "A",
        "data": {
            "key": "A",
            "title": "A revised metadata",
            "relations": {"dc:references": "http://zotero/items/C"},
        },
    }
    write_citation_sidecar(
        tmp_path,
        item=changed,
        source_id="source-zotero-a",
        text="",
        content_hash=content_hash,
        source_file="zotero://select/library/items/APDF",
        extraction_version="7",
    )
    refreshed = read_yaml(path)
    assert refreshed["reference_extraction_status"] == "succeeded"
    assert refreshed["references"] == original["references"]
    assert refreshed["zotero_relations"][0]["target_zotero_item_key"] == "C"
    assert refreshed["metadata_hash"] != original["metadata_hash"]
    assert refreshed["engine_version"] == "0.2.0"
    assert refreshed["artifact_schema_version"] == "1.1"


class _ChildFulltextClient:
    def __init__(self, text: str) -> None:
        self.text = text
        self.keys: list[str] = []

    def fulltext(self, item_key: str) -> Mapping[str, Any]:
        self.keys.append(item_key)
        return {"content": self.text}


def test_backfill_uses_recorded_child_attachment_key(tmp_path: Path) -> None:
    initialize_workspace(tmp_path)
    text = "References\nB Author. 2024. B. https://doi.org/10.8100/b"
    note = tmp_path / "02_source_memory" / "notes" / "seed.md"
    note.write_text(
        "---\n"
        "note_id: note-seed\n"
        "source_id: source-zotero-seed\n"
        "zotero_item_key: SEED\n"
        "title: Seed\n"
        "source_file: zotero://select/library/items/SEEDPDF\n"
        f"inspected_content_hash: {sha256_text(text)}\n"
        "extraction_version: '1'\n"
        "zotero_relations: {}\n"
        "original_zotero_tags: []\n"
        "---\n\n# Seed\n",
        encoding="utf-8",
    )
    client = _ChildFulltextClient(text)
    paths = backfill_citation_sidecars(tmp_path, client=client)  # type: ignore[arg-type]
    assert client.keys == ["SEEDPDF"]
    assert len(paths) == 1
    assert read_yaml(paths[0])["reference_extraction_status"] == "succeeded"


def test_nonaccepted_export_is_blocked_and_projection_titles_are_sanitized(tmp_path: Path) -> None:
    initialize_workspace(tmp_path)
    candidate = ExpansionCandidate.from_dict(
        {**_candidate().to_dict(), "title": "Bad\n[[Missing|Injected]]"}
    )
    _write_candidate(tmp_path, candidate)
    with pytest.raises(ValueError, match="accepted"):
        export_expansion_candidates(tmp_path, tmp_path / "bad.bib", state="proposed")
    render_expansion_projection(tmp_path)
    page = tmp_path / "03_literature_synthesis" / "expansion" / "candidates" / "suggestion-integrity.md"
    text = page.read_text(encoding="utf-8")
    assert "# Bad [Missing-Injected]" in text
    assert "[[Missing" not in text


def test_doctor_detects_incomplete_expansion_state(tmp_path: Path, sample_items) -> None:
    initialize_workspace(tmp_path)
    (tmp_path / "03_literature_synthesis" / "expansion" / "decisions.yml").unlink()
    report = doctor(tmp_path, client=FakeZotero(sample_items))
    assert report.checks["expansion"]["status"] == "incomplete"


class _UnversionedController:
    def review_expansion_candidates(self, candidates):
        return [
            {
                "suggestion_id": candidates[0]["suggestion_id"],
                "decision": "accepted",
                "decision_reason": "missing CAS version",
            }
        ]


def test_controller_decision_without_explicit_expected_version_is_parked(tmp_path: Path) -> None:
    initialize_workspace(tmp_path)
    _write_candidate(tmp_path, _candidate())
    decided = decide_expansion(
        tmp_path,
        ExpansionDecision("suggestion-integrity", 0, "accepted", "requested acceptance"),
        controller=_UnversionedController(),
    )
    assert decided.state == "parked"
    ledger = read_yaml(tmp_path / "03_literature_synthesis" / "expansion" / "decisions.yml")["decisions"]
    assert ledger[0]["reason"] == "controller_returned_no_unique_valid_decision"


def test_status_counts_expansion_without_mutating_registries(tmp_path: Path) -> None:
    initialize_workspace(tmp_path)
    _write_candidate(tmp_path, _candidate())
    candidates_path = tmp_path / "03_literature_synthesis" / "expansion" / "candidates.yml"
    decisions_path = tmp_path / "03_literature_synthesis" / "expansion" / "decisions.yml"
    before = (candidates_path.read_bytes(), decisions_path.read_bytes())
    status = get_status(tmp_path)
    assert status.counts["expansion_candidate_count"] == 1
    assert status.counts["expansion_proposed_count"] == 1
    assert (candidates_path.read_bytes(), decisions_path.read_bytes()) == before
