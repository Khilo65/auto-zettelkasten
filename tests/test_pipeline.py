from __future__ import annotations

import json
from pathlib import Path

import yaml

from auto_zettelkasten.api import build_map, export_to_obsidian, get_status, resume_map, run_map
from auto_zettelkasten.models import MapRequest
from auto_zettelkasten.notes import parse_atomic_note
from auto_zettelkasten.obsidian import _missing_links_from_contents

from conftest import FakeReader, FakeZotero


GOLDEN = yaml.safe_load((Path(__file__).parent / "golden" / "vertical_slice.yml").read_text())


def test_vertical_slice_matches_golden_and_builds_obsidian_graph(tmp_path: Path, sample_items) -> None:
    reader = FakeReader()
    report = run_map(
        MapRequest(tmp_path, scope="collection", collection_key="COLL1", provider="ollama", model="fake-1", parallel=2),
        client=FakeZotero(sample_items),
        reader=reader,
        run_id="golden-run",
    )
    assert report.inventory_count == GOLDEN["inventory_count"]
    assert report.validated_note_count == GOLDEN["validated_note_count"]
    assert report.exhausted_count == GOLDEN["exhausted_count"]
    assert report.terminal_count == GOLDEN["terminal_count"]
    assert len(report.cluster_map["clusters"]) == GOLDEN["cluster_count"]
    assert len(report.gap_map["gap_candidates"]) == GOLDEN["gap_candidate_count"]
    assert report.gap_map["gap_candidates"][0]["status"] == GOLDEN["gap_status"]
    assert report.gap_map["gap_candidates"][0]["novelty_claimed"] is GOLDEN["novelty_claimed"]
    assert report.gap_map["gap_candidates"][0]["closest_prior_work"]
    assert report.source_set["inventory_count"] == report.source_set["terminal_count"]
    assert report.source_set["original_zotero_tags"] == ["Exact Tag CASE", "Shared Topic"]
    assert report.source_set["normalized_tags"] == ["exact-tag-case", "shared-topic"]
    typed_links = yaml.safe_load((tmp_path / "02_source_memory" / "indexes" / "typed_links.yml").read_text())["links"]
    assert len(typed_links) == GOLDEN["typed_link_count"]
    assert {row["relation_type"] for row in typed_links} == {"cites", "same_concept"}
    canonical_gaps = yaml.safe_load((tmp_path / "03_literature_synthesis" / "gaps" / "gaps.yml").read_text())
    compatible_gaps = yaml.safe_load((tmp_path / "02_source_memory" / "indexes" / "gap_candidates.yml").read_text())
    assert compatible_gaps == canonical_gaps
    assert compatible_gaps["status"] == "candidate_only"
    assert compatible_gaps["novelty_claimed"] is False
    assert any(
        artifact["path"] == "02_source_memory/indexes/gap_candidates.yml"
        for artifact in report.artifact_manifest.artifacts
    )
    for relative in (
        "02_source_memory/indexes/INDEX.md",
        "02_source_memory/indexes/tag_registry.yml",
        "02_source_memory/indexes/tag_proposals.yml",
        "02_source_memory/indexes/typed_note_links.yml",
        "02_source_memory/indexes/gap_candidates.yml",
        "02_source_memory/indexes/source_sets/source-set-auto-zettelkasten-workspace.yml",
    ):
        assert (tmp_path / relative).is_file(), relative

    note_paths = sorted((tmp_path / "02_source_memory" / "notes").glob("*.md"))
    assert len(note_paths) == 2
    for path in note_paths:
        text = path.read_text()
        front, _ = parse_atomic_note(text)
        assert f"## {GOLDEN['required_note_section']}" in text
        assert front["note_status"] == "analytical_atomic_note"
        assert front["clusters"]
        assert {row["relation_type"] for row in front["related_notes"]}.issubset({"cites", "cited_by", "same_concept"})
        assert "## Graph Links" in text
        assert "[[" in text.split("## Graph Links", 1)[1]

    vault = tmp_path / "vault"
    export = export_to_obsidian(tmp_path, vault, new_vault=True)
    assert export.status == "exported"
    assert export.metadata["missing_wikilink_count"] == GOLDEN["missing_wikilink_count"]
    export_root = Path(export.metadata["export_root"])
    assert (export_root / "Home.md").exists()
    assert (export_root / "Indexes" / "Source Index.md").exists()
    assert (export_root / "Indexes" / "Cluster Index.md").exists()
    assert (export_root / "Indexes" / "Gap Index.md").exists()
    gap_id = report.gap_map["gap_candidates"][0]["gap_id"]
    assert (export_root / "Closest Prior Work" / f"closest-prior-{gap_id}.md").exists()


def test_obsidian_link_audit_rejects_ambiguous_unqualified_targets(tmp_path: Path) -> None:
    contents = {
        tmp_path / "Index.md": "[[duplicate]]\n",
        tmp_path / "First" / "duplicate.md": "# First\n",
        tmp_path / "Second" / "duplicate.md": "# Second\n",
    }

    assert _missing_links_from_contents(tmp_path, contents) == [
        {"source": "Index.md", "target": "duplicate", "reason": "ambiguous_target"}
    ]


def test_obsidian_export_repairs_folded_frontmatter_wikilinks(tmp_path: Path, sample_items) -> None:
    report = run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1", parallel=1),
        client=FakeZotero(sample_items[:1]),
        reader=FakeReader(),
        run_id="folded-frontmatter",
    )
    source_path = tmp_path / report.items[0]["note_path"]
    long_target = (
        "A deliberately long related note title that exceeds the default YAML wrapping width "
        "while remaining a valid Obsidian target"
    )
    target_path = source_path.parent / f"{long_target}.md"
    target_path.write_text(f"# {long_target}\n", encoding="utf-8")
    frontmatter, body = parse_atomic_note(source_path.read_text(encoding="utf-8"))
    frontmatter["related_notes"] = [
        {
            "note_id": "note-long-target",
            "relation_type": "same_concept",
            "wikilink": f"[[{long_target}]]",
        }
    ]
    wrapped = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    source_path.write_text(f"---\n{wrapped}\n---\n{body}", encoding="utf-8")
    assert not any(
        f"[[{long_target}]]" in line
        for line in source_path.read_text(encoding="utf-8").splitlines()
    )

    exported = export_to_obsidian(tmp_path, tmp_path / "vault", new_vault=True)

    assert exported.status == "exported"
    assert exported.metadata["missing_wikilink_count"] == 0
    projected = Path(exported.metadata["export_root"]) / "Sources" / source_path.name
    assert any(f"[[{long_target}]]" in line for line in projected.read_text(encoding="utf-8").splitlines())


def test_missing_attachment_and_duplicate_are_terminal_exhausted(tmp_path: Path, sample_items) -> None:
    items = [sample_items[0], sample_items[0], sample_items[1]]
    report = run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1", parallel=1),
        client=FakeZotero(items, missing={"ITEMB"}),
        reader=FakeReader(),
        run_id="coverage-run",
    )
    assert report.inventory_count == 3
    assert report.terminal_count == 3
    assert report.validated_note_count == 1
    assert report.exhausted_count == 2
    reasons = {row["reason"] for row in report.items if row["terminal_status"] == "exhausted"}
    assert reasons == {"duplicate_zotero_item_key", "all_allowed_extraction_routes_exhausted"}
    assert [row["terminal_status"] for row in report.source_set["rows"]] == ["validated_note", "exhausted", "exhausted"]
    attempts = (tmp_path / "01_custody" / "read_attempts" / "coverage-run.jsonl").read_text()
    assert "duplicate_zotero_item_key" in attempts
    assert "all_allowed_extraction_routes_exhausted" in attempts


def test_cloud_reader_is_never_called_without_explicit_consent(tmp_path: Path, sample_items) -> None:
    class CloudSpy(FakeReader):
        name = "cloud-spy"
        is_cloud = True

    spy = CloudSpy()
    report = run_map(
        MapRequest(tmp_path, provider="deepseek", model="spy", allow_cloud=False),
        client=FakeZotero(sample_items[:1]),
        reader=spy,
        run_id="private-run",
    )
    assert spy.calls == 0
    assert report.status == "blocked"
    assert report.inventory_count == 0
    assert report.errors[0]["reason"] == "cloud_reader_requires_allow_cloud:cloud-spy"


def test_fingerprint_rerun_and_same_run_resume_skip_reader(tmp_path: Path, sample_items) -> None:
    client = FakeZotero(sample_items)
    first_reader = FakeReader()
    request = MapRequest(tmp_path, provider="ollama", model="fake-1", parallel=1)
    run_map(request, client=client, reader=first_reader, run_id="first")
    assert first_reader.calls == 2

    second_reader = FakeReader()
    second = run_map(request, client=FakeZotero(sample_items), reader=second_reader, run_id="second")
    assert second.validated_note_count == 2
    assert second.reused_count == 2
    assert second_reader.calls == 0
    assert len(list((tmp_path / "02_source_memory" / "notes").glob("*.md"))) == 2

    resume_reader = FakeReader()
    resumed = resume_map(tmp_path, "first", client=FakeZotero(sample_items), reader=resume_reader)
    assert resumed.validated_note_count == 2
    assert resumed.reused_count == 2
    assert resume_reader.calls == 0


def test_resume_revalidates_item_identity_and_content_fingerprint(tmp_path: Path, sample_items) -> None:
    request = MapRequest(tmp_path, provider="ollama", model="fake-1", parallel=1)
    run_map(request, client=FakeZotero(sample_items), reader=FakeReader(), run_id="identity-run")
    replacement = {
        "key": "ITEMC",
        "data": {
            "key": "ITEMC",
            "itemType": "journalArticle",
            "title": "Replacement Item",
            "date": "2025",
            "creators": [{"lastName": "Three"}],
            "tags": [],
        },
    }
    reader = FakeReader()
    resumed = resume_map(tmp_path, "identity-run", client=FakeZotero([replacement, sample_items[1]]), reader=reader)
    assert reader.calls == 1
    assert resumed.reused_count == 1
    assert [row["zotero_item_key"] for row in resumed.items] == ["ITEMC", "ITEMB"]
    assert resumed.source_set["zotero_item_keys"] == ["ITEMC", "ITEMB"]
    assert all(row["source_id"] != "source-zotero-itema" for row in resumed.source_set["rows"])


def test_interrupted_run_resumes_only_missing_item_to_completion(tmp_path: Path, sample_items) -> None:
    request = MapRequest(tmp_path, provider="ollama", model="fake-1", parallel=1)
    completed = run_map(request, client=FakeZotero(sample_items), reader=FakeReader(), run_id="interrupted")
    missing = next(row for row in completed.items if row["zotero_item_key"] == "ITEMB")
    (tmp_path / missing["note_path"]).unlink()
    (tmp_path / "11_state" / "fingerprints" / f"{missing['fingerprint']}.yml").unlink()
    (tmp_path / "11_state" / "runs" / "interrupted" / "run_report.yml").unlink()

    reader = FakeReader()
    resumed = resume_map(tmp_path, "interrupted", client=FakeZotero(sample_items), reader=reader)
    assert resumed.status == "completed"
    assert resumed.inventory_count == resumed.terminal_count == 2
    assert resumed.validated_note_count == 2
    assert resumed.reused_count == 1
    assert reader.calls == 1


def test_untagged_source_can_commit_without_creating_cluster(tmp_path: Path, sample_items) -> None:
    sample_items[0]["data"]["tags"] = []
    report = run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1"),
        client=FakeZotero(sample_items[:1]),
        reader=FakeReader(),
        run_id="untagged",
    )
    assert report.validated_note_count == 1
    assert report.exhausted_count == 0
    assert report.source_set["original_zotero_tags"] == []
    assert report.cluster_map["clusters"] == []


def test_note_filename_collision_gets_stable_non_overwriting_suffix(tmp_path: Path, sample_items) -> None:
    collision = {**sample_items[0], "key": "OTHER", "data": {**sample_items[0]["data"], "key": "OTHER"}}
    report = run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1", parallel=2),
        client=FakeZotero([sample_items[0], collision]),
        reader=FakeReader(),
        run_id="collision",
    )
    assert report.inventory_count == report.terminal_count == 2
    assert report.validated_note_count == 2
    assert report.exhausted_count == 0
    names = sorted(path.name for path in (tmp_path / "02_source_memory" / "notes").glob("*.md"))
    assert len(names) == 2
    assert any("[other]" in name for name in names)


def test_question_metadata_and_effective_reader_are_part_of_fingerprint(tmp_path: Path, sample_items) -> None:
    class QuestionReader(FakeReader):
        name = "actual-reader"
        model = "actual-v1"

        def read_source(self, text, metadata, question=None):
            self.calls += 1
            return {key: f"{question} / {metadata['title']} / page 1" for key in __import__("conftest").SECTION_KEYS}

    first_reader = QuestionReader()
    first = run_map(
        MapRequest(tmp_path, provider="ollama", model="requested-model", question="FIRST"),
        client=FakeZotero(sample_items[:1]),
        reader=first_reader,
        run_id="question-first",
    )
    note_path = tmp_path / first.items[0]["note_path"]
    front, _ = parse_atomic_note(note_path.read_text())
    assert front["reader_provider"] == "actual-reader"
    assert front["reader_model"] == "actual-v1"

    changed = [json.loads(json.dumps(sample_items[0]))]
    changed[0]["data"]["title"] = "Changed Metadata Title"
    second_reader = QuestionReader()
    second = run_map(
        MapRequest(tmp_path, provider="ollama", model="requested-model", question="SECOND"),
        client=FakeZotero(changed),
        reader=second_reader,
        run_id="question-second",
    )
    assert second.reused_count == 0
    assert second_reader.calls == 1
    text = (tmp_path / second.items[0]["note_path"]).read_text()
    assert "SECOND / Changed Metadata Title" in text


def test_long_document_uses_bounded_chunk_reader_route(tmp_path: Path, sample_items) -> None:
    class LongZotero(FakeZotero):
        def fulltext(self, item_key):
            if item_key.endswith("PDF"):
                return {"content": "Long grounded paragraph. " * 16000}
            return None

    reader = FakeReader()
    report = run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1", parallel=1),
        client=LongZotero(sample_items[:1]),
        reader=reader,
        run_id="long-source",
    )
    assert report.validated_note_count == 1
    assert reader.calls > 1
    attempts = (tmp_path / "01_custody" / "read_attempts" / "long-source.jsonl").read_text()
    assert "fake-reader_chunked_text" in attempts


def test_partial_zotero_fulltext_is_not_treated_as_exhaustive(tmp_path: Path, sample_items) -> None:
    class PartialZotero(FakeZotero):
        def fulltext(self, item_key):
            if item_key.endswith("PDF"):
                return {"content": "Only one indexed page. " * 10, "indexedPages": 1, "totalPages": 100}
            return None

    client = PartialZotero(sample_items[:1])
    reader = FakeReader()
    report = run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1"),
        client=client,
        reader=reader,
        run_id="partial-fulltext",
    )
    assert report.exhausted_count == 1
    assert reader.calls == 0
    assert client.file_calls == 1
    attempts = (tmp_path / "01_custody" / "read_attempts" / "partial-fulltext.jsonl").read_text()
    assert "partial_indexed_fulltext" in attempts


def test_synthetic_library_covers_readable_scanned_and_missing_sources(tmp_path: Path) -> None:
    items = json.loads((Path(__file__).parent / "fixtures" / "library.json").read_text())

    class ReleaseGateZotero(FakeZotero):
        def children(self, item_key):
            if item_key == "MISSING1":
                return []
            return [{"key": item_key + "PDF", "data": {"key": item_key + "PDF", "itemType": "attachment", "contentType": "application/pdf", "filename": item_key + ".pdf"}}]

        def fulltext(self, item_key):
            if item_key == "READABLE1PDF":
                return {"content": "Readable inspected synthetic source. " * 30}
            return None

        def file(self, item_key):
            self.file_calls += 1
            if item_key == "SCANNED1PDF":
                return b"%PDF-1.4\n%%EOF\n", "application/pdf"
            return None

    report = run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1", parallel=1),
        client=ReleaseGateZotero(items),
        reader=FakeReader(),
        run_id="synthetic-release-gate",
    )
    assert report.inventory_count == report.terminal_count == 3
    assert report.validated_note_count == 1
    assert report.exhausted_count == 2
    attempts = (tmp_path / "01_custody" / "read_attempts" / "synthetic-release-gate.jsonl").read_text()
    assert '"route": "local_ocr"' in attempts
    assert "all_allowed_extraction_routes_exhausted" in attempts


def test_workspace_map_remains_coherent_across_collection_runs(tmp_path: Path, sample_items) -> None:
    request = MapRequest(tmp_path, provider="ollama", model="fake-1")
    run_map(request, client=FakeZotero(sample_items), reader=FakeReader(), run_id="workspace-one")
    second = run_map(request, client=FakeZotero(sample_items[:1]), reader=FakeReader(), run_id="workspace-two")
    assert len(second.cluster_map["clusters"]) == 1
    assert len(list((tmp_path / "03_literature_synthesis" / "clusters").glob("cluster-*.md"))) == 1
    assert len(list((tmp_path / "03_literature_synthesis" / "gaps" / "candidates").glob("gap-*.md"))) == 1
    exported = export_to_obsidian(tmp_path, tmp_path / "vault", new_vault=True)
    assert exported.metadata["missing_wikilink_count"] == 0


def test_existing_custody_relations_feed_compatible_typed_note_links(tmp_path: Path, sample_items) -> None:
    import csv

    for item in sample_items:
        item["data"]["tags"] = []
        item["data"]["relations"] = {}
    report = run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1"),
        client=FakeZotero(sample_items),
        reader=FakeReader(),
        run_id="custody-relations",
    )
    relation_path = tmp_path / "01_custody" / "source_relation_registry.csv"
    with relation_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["relation_id", "source_id", "related_source_id", "relation_type", "route", "confidence", "created_at"])
        writer.writeheader()
        writer.writerow({"relation_id": "rel-1", "source_id": report.items[0]["source_id"], "related_source_id": report.items[1]["source_id"], "relation_type": "closest_prior_work", "route": "fixture", "confidence": "high", "created_at": "2026-01-01"})
    limited_path = tmp_path / report.items[1]["note_path"]
    limited_path.write_text(limited_path.read_text().replace("note_status: analytical_atomic_note", "note_status: fulltext_available", 1))
    build_map(tmp_path, run_id="custody-rebuild")
    compatibility = yaml.safe_load((tmp_path / "02_source_memory" / "indexes" / "typed_note_links.yml").read_text())
    assert compatibility["links"][0]["relation_type"] == "closest_prior_work"
    assert compatibility["links"][0]["provenance"] == "01_custody/source_relation_registry.csv"


def test_status_uses_run_snapshot_and_reports_missing_run(tmp_path: Path, sample_items) -> None:
    run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1"),
        client=FakeZotero(sample_items),
        reader=FakeReader(),
        run_id="snapshot-run",
    )
    (tmp_path / "03_literature_synthesis" / "clusters" / "clusters.yml").write_text("clusters: []\n")
    assert get_status(tmp_path, "snapshot-run").counts["cluster_count"] == 1
    missing = get_status(tmp_path, "does-not-exist")
    assert missing.status == "blocked"
    assert missing.message == "run_not_found"


def test_selected_collection_key_and_limit_are_preserved(tmp_path: Path, sample_items) -> None:
    client = FakeZotero(sample_items, selected_key="ACTUALSELECTED")
    report = run_map(
        MapRequest(tmp_path, scope="selected", provider="ollama", model="fake-1", limit=1),
        client=client,
        reader=FakeReader(),
        run_id="selected-run",
    )
    assert client.inventory_calls == [("collection", "ACTUALSELECTED")]
    assert report.inventory_count == 1
    assert report.source_set["zotero_collection_key"] == "ACTUALSELECTED"
    assert report.source_set["source_set_type"] == "zotero_collection"


def test_status_reports_terminal_and_literature_counts(tmp_path: Path, sample_items) -> None:
    run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1"),
        client=FakeZotero(sample_items),
        reader=FakeReader(),
        run_id="status-run",
    )
    status = get_status(tmp_path, "status-run")
    assert status.status == "completed"
    assert status.counts["inventory_count"] == status.counts["terminal_count"] == 2
    assert status.counts["cluster_count"] == 1
    assert status.counts["gap_candidate_count"] == 1
