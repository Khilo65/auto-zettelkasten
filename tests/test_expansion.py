from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import shutil
from typing import Any, Mapping, Sequence

import pytest

from auto_zettelkasten.api import (
    decide_expansion,
    export_expansion_candidates,
    export_to_obsidian,
    initialize_workspace,
    list_expansion_candidates,
    map_accepted_candidates,
    migrate_workspace,
    resume_expansion,
    run_expansion,
    run_map,
)
from auto_zettelkasten.files import read_yaml, write_yaml
from auto_zettelkasten.models import ExpansionDecision, ExpansionRequest, MapRequest
from auto_zettelkasten.workspace import IncompatibleArtifactSchemaError

from conftest import FakeReader, FakeZotero


class NetworkGraphSpy:
    name = "semantic-scholar"
    is_network = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.attempts: list[dict[str, Any]] = []

    def resolve_work(self, work: Mapping[str, Any]) -> Mapping[str, Any] | None:
        self.calls.append(("resolve", dict(work)))
        self.attempts.append({"provider": self.name, "status": "succeeded", "request_hash": "resolve"})
        return {
            **dict(work),
            "provider_ids": {"semantic_scholar": "S2-SEED"},
        }

    def citation_neighbors(self, work: Mapping[str, Any], *, limit: int) -> Sequence[Mapping[str, Any]]:
        self.calls.append(("neighbors", {"work": dict(work), "limit": limit}))
        self.attempts.append({"provider": self.name, "status": "succeeded", "request_hash": "neighbors"})
        return [
            {
                "title": "External Citation Candidate",
                "year": "2025",
                "authors": ["Cara Three"],
                "doi": "10.5555/external-candidate",
                "provider_ids": {"semantic_scholar": "S2-CANDIDATE"},
                "relation_type": "cited_by",
                "provider_relevance": 0.9,
            }
        ][:limit]

    def recommendations(
        self,
        positive_paper_ids: Sequence[str],
        *,
        negative_paper_ids: Sequence[str] = (),
        limit: int,
    ) -> Sequence[Mapping[str, Any]]:
        self.calls.append(
            (
                "recommendations",
                {
                    "positive_paper_ids": list(positive_paper_ids),
                    "negative_paper_ids": list(negative_paper_ids),
                    "limit": limit,
                },
            )
        )
        return []

    def drain_attempts(self) -> Sequence[Mapping[str, Any]]:
        rows = list(self.attempts)
        self.attempts.clear()
        return rows


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _seed_workspace(workspace: Path, sample_items: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    items = deepcopy(sample_items)
    items[0]["data"]["DOI"] = "10.5555/seed"
    items[1]["data"]["DOI"] = "10.5555/related"
    seed_report = run_map(
        MapRequest(workspace, provider="ollama", model="fake-1", parallel=1),
        client=FakeZotero(items[:1]),
        reader=FakeReader(),
        run_id="seed-map",
    )
    assert seed_report.validated_note_count == 1
    return str(seed_report.items[0]["source_id"]), items


def _local_expansion(
    workspace: Path,
    sample_items: list[dict[str, Any]],
    *,
    run_id: str = "local-expansion",
) -> tuple[Any, list[dict[str, Any]]]:
    source_id, items = _seed_workspace(workspace, sample_items)
    report = run_expansion(
        ExpansionRequest(
            workspace,
            scope="source",
            target_ids=(source_id,),
            provider="internal",
            depth=1,
            budget=10,
            run_id=run_id,
        ),
        client=FakeZotero(items),
    )
    return report, items


def test_schema_migration_is_explicit_dry_run_safe_and_idempotent(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    initialize_workspace(workspace)
    note = workspace / "02_source_memory" / "notes" / "existing.md"
    note.write_text("---\nnote_id: existing\n---\n\n# Existing\n", encoding="utf-8")
    manifest_path = workspace / "11_state" / "workspace_manifest.yml"
    manifest = read_yaml(manifest_path)
    manifest.update(engine_version="0.1.0", artifact_schema_version="1.0")
    write_yaml(manifest_path, manifest)
    shutil.rmtree(workspace / "01_custody" / "citation_leads")
    shutil.rmtree(workspace / "03_literature_synthesis" / "expansion")

    with pytest.raises(IncompatibleArtifactSchemaError, match="migrate"):
        run_expansion(
            ExpansionRequest(workspace, scope="source", target_ids=("source-existing",)),
            client=FakeZotero([]),
        )

    before = _tree_bytes(workspace)
    dry_run = migrate_workspace(workspace, target="1.1", dry_run=True)
    assert dry_run.status == "dry_run"
    assert dry_run.metadata["from_version"] == "1.0"
    assert dry_run.metadata["to_version"] == "1.1"
    assert dry_run.metadata["canonical_notes_rewritten"] is False
    assert _tree_bytes(workspace) == before

    migrated = migrate_workspace(workspace, target="1.1")
    assert migrated.status == "migrated"
    assert read_yaml(manifest_path)["artifact_schema_version"] == "1.1"
    assert note.read_bytes() == before[str(note.relative_to(workspace))]
    candidates_path = workspace / "03_literature_synthesis" / "expansion" / "candidates.yml"
    decisions_path = workspace / "03_literature_synthesis" / "expansion" / "decisions.yml"
    assert read_yaml(candidates_path)["candidates"] == []
    assert read_yaml(decisions_path)["decisions"] == []

    after_first = _tree_bytes(workspace)
    repeated = migrate_workspace(workspace, target="1.1")
    assert repeated.status == "already_current"
    assert _tree_bytes(workspace) == after_first


def test_local_expansion_has_stable_ids_ranking_and_versioned_lifecycle(tmp_path: Path, sample_items) -> None:
    report, items = _local_expansion(tmp_path, sample_items)
    assert report.status == "completed"
    assert report.seed_count == 1
    assert report.candidate_count >= 1
    related = next(row for row in report.candidates if row["zotero_item_key"] == "ITEMB")
    assert related["primary_relation"] == "cites"
    assert related["ranking"]["relation_strength"] == 1.0
    assert related["state"] == "proposed"
    assert related["decision_version"] == 0

    rerun = run_expansion(
        ExpansionRequest(
            tmp_path,
            scope="source",
            target_ids=("source-zotero-itema",),
            provider="internal",
            depth=1,
            budget=10,
            run_id="local-expansion-rerun",
        ),
        client=FakeZotero(items),
    )
    assert [row["suggestion_id"] for row in rerun.candidates] == [row["suggestion_id"] for row in report.candidates]
    assert [row["score"] for row in rerun.candidates] == [row["score"] for row in report.candidates]
    stored = list_expansion_candidates(tmp_path)
    assert len({row.suggestion_id for row in stored}) == len(stored)

    accepted = decide_expansion(
        tmp_path,
        ExpansionDecision(
            suggestion_id=related["suggestion_id"],
            expected_version=0,
            decision="accepted",
            reason="Relevant direct citation",
            actor="test-user",
        ),
    )
    assert accepted.state == "accepted"
    assert accepted.decision_version == 1
    with pytest.raises(RuntimeError, match="version"):
        decide_expansion(
            tmp_path,
            ExpansionDecision(
                suggestion_id=related["suggestion_id"],
                expected_version=0,
                decision="rejected",
                reason="Stale review must not overwrite",
            ),
        )
    decisions = read_yaml(tmp_path / "03_literature_synthesis" / "expansion" / "decisions.yml")["decisions"]
    assert [(row["decision"], row["expected_version"]) for row in decisions] == [("accepted", 0)]


def test_network_provider_requires_consent_again_on_resume_and_exports_metadata(
    tmp_path: Path,
    sample_items,
) -> None:
    source_id, _ = _seed_workspace(tmp_path, sample_items)
    spy = NetworkGraphSpy()
    with pytest.raises(ValueError, match="allow_network"):
        ExpansionRequest(
            tmp_path,
            scope="source",
            target_ids=(source_id,),
            provider="semantic-scholar",
            allow_network=False,
        )
    assert spy.calls == []

    report = run_expansion(
        ExpansionRequest(
            tmp_path,
            scope="source",
            target_ids=(source_id,),
            provider="semantic-scholar",
            allow_network=True,
            budget=10,
            run_id="external-expansion",
        ),
        graph_provider=spy,
        client=FakeZotero([]),
    )
    external = next(row for row in report.candidates if row["doi"] == "10.5555/external-candidate")
    assert spy.calls[0] == ("resolve", {"doi": "10.5555/seed"})
    outbound = repr(spy.calls)
    for forbidden in ("Source-grounded", "inspected_content_hash", "source_file", "original_zotero_tags"):
        assert forbidden not in outbound
    calls_after_run = len(spy.calls)
    with pytest.raises(ValueError, match="allow_network"):
        resume_expansion(tmp_path, "external-expansion", graph_provider=spy)
    assert len(spy.calls) == calls_after_run

    resumed = resume_expansion(tmp_path, "external-expansion", allow_network=True, graph_provider=spy)
    assert resumed.status == "completed"
    assert len(spy.calls) == calls_after_run
    provider_cache = read_yaml(
        tmp_path / "11_state" / "runs" / "external-expansion" / "provider_results.yml"
    )
    assert provider_cache["calls"]

    accepted = decide_expansion(
        tmp_path,
        ExpansionDecision(
            suggestion_id=external["suggestion_id"],
            expected_version=0,
            decision="accepted",
            reason="Export for acquisition review",
        ),
    )
    assert accepted.state == "accepted"
    bibtex = tmp_path / "accepted.bib"
    bib_result = export_expansion_candidates(tmp_path, state="accepted", format="bibtex", output=bibtex)
    assert bib_result.status == "exported"
    assert "10.5555/external-candidate" in bibtex.read_text(encoding="utf-8")
    ris = tmp_path / "accepted.ris"
    ris_result = export_expansion_candidates(tmp_path, state="accepted", format="ris", output=ris)
    assert ris_result.status == "exported"
    ris_text = ris.read_text(encoding="utf-8")
    assert "DO  - 10.5555/external-candidate" in ris_text
    assert "TI  - External Citation Candidate" in ris_text


def test_map_accepted_processes_only_local_candidate(tmp_path: Path, sample_items) -> None:
    report, items = _local_expansion(tmp_path, sample_items)
    local = next(row for row in report.candidates if row["zotero_item_key"] == "ITEMB")
    decide_expansion(
        tmp_path,
        ExpansionDecision(
            suggestion_id=local["suggestion_id"],
            expected_version=0,
            decision="accepted",
            reason="Acquire the local related item",
        ),
    )

    mapped = map_accepted_candidates(
        tmp_path,
        suggestion_ids=(local["suggestion_id"],),
        client=FakeZotero(items),
        reader=FakeReader(),
        provider="ollama",
        model="fake-1",
    )
    assert mapped.status == "completed"
    candidate = next(row for row in list_expansion_candidates(tmp_path) if row.suggestion_id == local["suggestion_id"])
    assert candidate.fulfillment == "mapped"
    assert any("ITEMB" in path.read_text(encoding="utf-8") for path in (tmp_path / "02_source_memory" / "notes").glob("*.md"))


def test_obsidian_projection_includes_expansion_indexes_and_has_no_broken_links(tmp_path: Path, sample_items) -> None:
    report, _ = _local_expansion(tmp_path, sample_items)
    candidate = report.candidates[0]
    canonical = (
        tmp_path
        / "03_literature_synthesis"
        / "expansion"
        / "candidates"
        / f"{candidate['suggestion_id']}.md"
    )
    assert canonical.exists()

    vault = tmp_path / "vault"
    exported = export_to_obsidian(tmp_path, vault, new_vault=True)
    assert exported.status == "exported"
    assert exported.metadata["missing_wikilink_count"] == 0
    assert exported.metadata["expansion_candidate_count"] == report.candidate_count
    export_root = Path(exported.metadata["export_root"])
    for relative in (
        "Expansion/Inbox.md",
        "Expansion/Accepted.md",
        "Expansion/Parked.md",
        "Expansion/Rejected.md",
    ):
        assert (export_root / relative).exists(), relative
    projected = export_root / "Expansion" / "Candidates" / canonical.name
    assert projected.read_bytes() == canonical.read_bytes()
    assert "[[Expansion/Inbox|Expansion Inbox]]" in (export_root / "Home.md").read_text(encoding="utf-8")


def test_cluster_projection_lists_suggestions_as_nonmembers(tmp_path: Path, sample_items) -> None:
    items = deepcopy(sample_items)
    items[0]["data"]["DOI"] = "10.5555/cluster-seed-a"
    items[1]["data"]["DOI"] = "10.5555/cluster-seed-b"
    mapped = run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1", parallel=1),
        client=FakeZotero(items),
        reader=FakeReader(),
        run_id="cluster-navigation-seeds",
    )
    [cluster] = mapped.cluster_map["clusters"]
    cluster_registry = tmp_path / "03_literature_synthesis" / "clusters" / "clusters.yml"
    registry_before = cluster_registry.read_bytes()
    third = deepcopy(items[1])
    third["key"] = "ITEMC"
    third["data"].update(
        {
            "key": "ITEMC",
            "title": "Cluster Expansion Candidate",
            "DOI": "10.5555/cluster-candidate",
            "relations": {},
        }
    )

    expanded = run_expansion(
        ExpansionRequest(
            tmp_path,
            scope="cluster",
            target_ids=(cluster["cluster_id"],),
            provider="internal",
            run_id="cluster-navigation-expansion",
        ),
        client=FakeZotero([*items, third]),
    )

    candidate = next(row for row in expanded.candidates if row["zotero_item_key"] == "ITEMC")
    cluster_page = (
        tmp_path
        / "03_literature_synthesis"
        / "clusters"
        / f"{cluster['cluster_id']}.md"
    ).read_text(encoding="utf-8")
    assert "## Expansion Suggestions (Non-Members)" in cluster_page
    assert candidate["suggestion_id"] in cluster_page
    assert f"discovery `{candidate['primary_relation']}`" in cluster_page
    assert "decision `proposed`; non-member" in cluster_page
    assert cluster_registry.read_bytes() == registry_before
    assert len(read_yaml(cluster_registry)["clusters"][0]["note_ids"]) == 2

    run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1", parallel=1),
        client=FakeZotero(items),
        reader=FakeReader(),
        run_id="cluster-navigation-normal-remap",
    )
    remapped_cluster_page = (
        tmp_path
        / "03_literature_synthesis"
        / "clusters"
        / f"{cluster['cluster_id']}.md"
    ).read_text(encoding="utf-8")
    assert candidate["suggestion_id"] in remapped_cluster_page
    assert "decision `proposed`; non-member" in remapped_cluster_page

    exported = export_to_obsidian(tmp_path, tmp_path / "vault", new_vault=True)
    assert exported.status == "exported"
    assert exported.metadata["missing_wikilink_count"] == 0
