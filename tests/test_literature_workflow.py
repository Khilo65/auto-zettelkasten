from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from auto_zettelkasten.api import build_map, get_status, resume_map, run_literature_map, run_map
from auto_zettelkasten.models import LiteratureMappingPolicy, LiteratureMapRequest, MapRequest

from conftest import FakeReader, FakeZotero


def literature_profile(source_id: str, *, topic: str = "institutional trust") -> dict:
    return {
        "source_id": source_id,
        "note_id": f"note-{source_id}",
        "title": f"{topic.title()} in {source_id.upper()}",
        "note_status": "analytical_atomic_note",
        "study_family_id": f"family-{source_id}",
        "semantic_topics": [topic],
        "outcomes": [f"{topic} outcome"],
        "findings": [
            {
                "claim_id": f"claim-{source_id}",
                "claim": f"{topic} has a positive result.",
                "topic": topic,
                "direction": "positive",
                "locator": "p. 10",
            }
        ],
    }


def test_canonical_map_replay_is_idempotent_and_uses_profile_sidecars(tmp_path: Path, sample_items) -> None:
    first_report = run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1"),
        client=FakeZotero(sample_items),
        reader=FakeReader(),
        run_id="canonical-first",
    )
    note_bytes = {path.name: path.read_bytes() for path in (tmp_path / "02_source_memory" / "notes").glob("*.md")}
    first_maps = sorted((tmp_path / "03_literature_synthesis" / "maps").glob("*/manifest.yml"))
    assert len(first_maps) == 1
    first_map_id = first_maps[0].parent.name
    rebuilt = build_map(
        tmp_path,
        run_id="canonical-replay",
        source_set=first_report.source_set,
        provider="ollama",
        model="fake-1",
    )

    assert rebuilt.status == "built"
    assert first_map_id in {path.parent.name for path in (tmp_path / "03_literature_synthesis" / "maps").glob("*/manifest.yml")}
    assert {path.name: path.read_bytes() for path in (tmp_path / "02_source_memory" / "notes").glob("*.md")} == note_bytes
    manifest = yaml.safe_load(first_maps[0].read_text())
    assert manifest["source_set_id"]
    assert manifest["note_projection_hashes"]


def test_resume_uses_frozen_inventory_and_source_representations(tmp_path: Path, sample_items) -> None:
    request = MapRequest(tmp_path, provider="ollama", model="fake-1", parallel=1)
    run_map(request, client=FakeZotero(sample_items), reader=FakeReader(), run_id="frozen-resume")
    changed_client = FakeZotero([sample_items[1]])
    reader = FakeReader()

    resumed = resume_map(tmp_path, "frozen-resume", client=changed_client, reader=reader)

    assert resumed.status == "completed"
    assert changed_client.inventory_calls == []
    assert changed_client.children_calls == 0
    assert changed_client.fulltext_calls == 0
    assert reader.calls == 0
    assert resumed.source_set["zotero_item_keys"] == ["ITEMA", "ITEMB"]
    assert len(list((tmp_path / "11_state" / "runs" / "frozen-resume" / "reports").glob("run-report-*.yml"))) == 2


def test_corrupt_profile_checkpoint_returns_resumable_partial(tmp_path: Path, sample_items) -> None:
    request = MapRequest(tmp_path, provider="ollama", model="fake-1", parallel=1)
    run_map(request, client=FakeZotero(sample_items), reader=FakeReader(), run_id="corrupt-profile")
    checkpoint = next((tmp_path / "11_state" / "runs" / "corrupt-profile" / "literature" / "profile_calls").glob("*.yml"))
    checkpoint.write_text("checkpoint_schema_version: '1'\nunexpected: true\n", encoding="utf-8")

    resumed = resume_map(tmp_path, "corrupt-profile", client=FakeZotero([]), reader=FakeReader())

    assert resumed.status == "partial"
    assert resumed.literature_report["partial_reason"] == "literature_profiling_partial:1_profile_failure"
    assert resumed.literature_failure_count == 1
    assert resumed.partial_count == 0
    assert resumed.pending_count == 0
    assert any(error.get("stage") == "literature_mapping" for error in resumed.errors)


def test_external_discovery_mode_requires_an_injected_provider_before_inventory(tmp_path: Path, sample_items) -> None:
    client = FakeZotero(sample_items)
    report = run_map(
        MapRequest(
            tmp_path,
            provider="ollama",
            model="fake-1",
            literature_policy=LiteratureMappingPolicy(external_discovery="per_run"),
        ),
        client=client,
        reader=FakeReader(),
        run_id="external-disabled",
    )
    assert report.status == "blocked"
    assert report.errors[0]["reason"] == "external_discovery_disabled_in_standalone_mapper:per_run"
    assert client.inventory_calls == []


def test_completed_status_exposes_literature_stage_and_counts(tmp_path: Path, sample_items) -> None:
    run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1"),
        client=FakeZotero(sample_items),
        reader=FakeReader(),
        run_id="literature-status",
    )
    status = get_status(tmp_path, "literature-status")
    assert status.checks["progress"]["stage"] == "reporting"
    assert {
        "preflight",
        "frozen_inventory",
        "source_processing",
        "profiling",
        "relation_mapping",
        "clustering",
        "evidence_matrices",
        "debate_mapping",
        "gap_detection",
        "internal_falsification",
        "projection",
        "reporting",
    } <= set(status.checks["progress"]["stage_timestamps"])
    assert status.counts["profile_count"] == 2
    assert status.counts["cluster_count"] == 1
    assert status.counts["mapped_gap_count"] == 0


def test_build_map_rejects_non_boolean_cloud_consent(tmp_path: Path) -> None:
    from auto_zettelkasten.api import initialize_workspace

    initialize_workspace(tmp_path)
    with pytest.raises(ValueError, match="allow_cloud must be a boolean"):
        build_map(tmp_path, allow_cloud="false")  # type: ignore[arg-type]


def test_source_set_alias_keeps_one_map_and_ledger_across_dependency_revisions(tmp_path: Path) -> None:
    alias = "source-set-zotero-collection-alpha"
    first_source_set = {
        "source_set_id": f"{alias}-snapshot-one",
        "source_set_alias": alias,
        "dependency_hash": "dependency-one",
    }
    second_source_set = {
        "source_set_id": f"{alias}-snapshot-two",
        "source_set_alias": alias,
        "dependency_hash": "dependency-two",
    }
    third_source_set = {
        "source_set_id": f"{alias}-snapshot-three",
        "source_set_alias": alias,
        "dependency_hash": "dependency-three",
    }
    first = run_literature_map(
        LiteratureMapRequest(workspace=tmp_path, source_set_id=first_source_set["source_set_id"], run_id="alias-first"),
        profiles=[literature_profile(source_id) for source_id in ("a", "b", "c")],
        source_set=first_source_set,
    )
    second = run_literature_map(
        LiteratureMapRequest(workspace=tmp_path, source_set_id=second_source_set["source_set_id"], run_id="alias-second"),
        profiles=[literature_profile(source_id) for source_id in ("a", "b", "c", "d")],
        source_set=second_source_set,
    )
    third = run_literature_map(
        LiteratureMapRequest(workspace=tmp_path, source_set_id=third_source_set["source_set_id"], run_id="alias-third"),
        profiles=[literature_profile(source_id) for source_id in ("a", "c", "d")],
        source_set=third_source_set,
    )

    assert first.map_id == second.map_id == third.map_id
    ledger = yaml.safe_load((tmp_path / third.artifact_paths["cluster_ledger"]).read_text())
    revisions = [row for row in ledger["events"] if row["event"] == "revision"]
    assert any(row.get("added_source_ids") == ["d"] for row in revisions)
    assert any(row.get("removed_source_ids") == ["b"] for row in revisions)

    other_alias = "source-set-zotero-collection-beta"
    other_source_set = {
        "source_set_id": f"{other_alias}-snapshot-one",
        "source_set_alias": other_alias,
        "dependency_hash": "dependency-three",
    }
    other = run_literature_map(
        LiteratureMapRequest(workspace=tmp_path, source_set_id=other_source_set["source_set_id"], run_id="alias-other"),
        profiles=[literature_profile("x", topic="ocean salinity"), literature_profile("y", topic="ocean salinity")],
        source_set=other_source_set,
    )
    assert other.map_id != first.map_id
    assert other.artifact_paths["cluster_ledger"] != first.artifact_paths["cluster_ledger"]
