from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
import yaml

from auto_zettelkasten.api import (
    _progress_items_from_source_set,
    build_map,
    export_to_obsidian,
    get_status,
    resume_map,
    run_map,
)
from auto_zettelkasten.literature import cluster_display_title, cluster_note_stem
from auto_zettelkasten.models import MapRequest, ProcessingPolicy
from auto_zettelkasten.notes import (
    parse_atomic_note,
    read_note,
    update_note_frontmatter,
)
from auto_zettelkasten.pipeline import _RunProgress

from conftest import FakeReader, FakeZotero


GOLDEN = yaml.safe_load(
    (Path(__file__).parent / "golden" / "vertical_slice.yml").read_text()
)


def test_vertical_slice_matches_golden_and_builds_obsidian_graph(
    tmp_path: Path, sample_items
) -> None:
    reader = FakeReader()
    report = run_map(
        MapRequest(
            tmp_path,
            scope="collection",
            collection_key="COLL1",
            provider="ollama",
            model="fake-1",
            parallel=2,
        ),
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
    assert report.gap_map["status"] == GOLDEN["gap_status"]
    assert report.gap_map["novelty_claimed"] is GOLDEN["novelty_claimed"]
    assert report.source_set["inventory_count"] == report.source_set["terminal_count"]
    assert report.source_set["original_zotero_tags"] == [
        "Exact Tag CASE",
        "Shared Topic",
    ]
    assert report.source_set["normalized_tags"] == ["exact-tag-case", "shared-topic"]
    typed_links = yaml.safe_load(
        (tmp_path / "02_source_memory" / "indexes" / "typed_links.yml").read_text()
    )["links"]
    assert len(typed_links) == GOLDEN["typed_link_count"]
    assert {row["relation_type"] for row in typed_links} == {"cites", "cited_by"}
    canonical_gaps = yaml.safe_load(
        (tmp_path / "03_literature_synthesis" / "gaps" / "gaps.yml").read_text()
    )
    compatible_gaps = yaml.safe_load(
        (tmp_path / "02_source_memory" / "indexes" / "gap_candidates.yml").read_text()
    )
    assert compatible_gaps == canonical_gaps
    assert compatible_gaps["status"] == "complete_no_qualifying_gaps"
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
        "02_source_memory/indexes/source_sets/source-set-zotero-coll1.yml",
    ):
        assert (tmp_path / relative).is_file(), relative

    note_paths = sorted((tmp_path / "02_source_memory" / "notes").glob("*.md"))
    assert len(note_paths) == 2
    for path in note_paths:
        text = path.read_text()
        projected, _ = parse_atomic_note(text)
        front = read_note(path)["frontmatter"]
        assert f"## {GOLDEN['required_note_section']}" in text
        assert projected["type"] == "atomic-note"
        assert projected["coverage"] == "full text"
        assert "content_route" not in projected
        assert "coverage_metrics" not in projected
        assert "normalized_tags" not in projected
        assert projected["zotero_tags"]
        assert projected.get("tags", []) == front["tags"]
        assert front["note_status"] == "analytical_atomic_note"
        assert front["clusters"] == []
        assert all(not tag.startswith("auto-zettelkasten/") for tag in front["tags"])
        assert "shared-topic" in front["normalized_tags"]
        expected_cluster_links = [
            f"[[{cluster_note_stem(cluster)}|{cluster_display_title(cluster)}]]"
            for cluster in report.cluster_map["clusters"]
            if cluster["cluster_id"] in front["clusters"]
        ]
        assert front["cluster_links"] == expected_cluster_links
        assert front["gap_links"] == []
        assert {row["relation_type"] for row in front["related_notes"]}.issubset(
            {"cites", "cited_by", "same_proposition", "semantic_similarity"}
        )
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
    assert list((export_root / "Clusters").glob("Cluster - *.md")) == []
    markdown_stems = [path.stem for path in export_root.rglob("*.md")]
    assert len(markdown_stems) == len(set(markdown_stems))
    assert not (export_root / "Literature Map").exists()
    assert len(list((export_root / "Indexes").glob("Literature Map - *.md"))) == 1


def test_missing_attachment_becomes_limited_and_duplicate_becomes_alias(
    tmp_path: Path, sample_items
) -> None:
    items = [sample_items[0], sample_items[0], sample_items[1]]
    reader = FakeReader()
    report = run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1", parallel=1),
        client=FakeZotero(items, missing={"ITEMB"}),
        reader=reader,
        run_id="coverage-run",
    )
    assert report.inventory_count == 3
    assert report.terminal_count == 3
    assert report.validated_note_count == 1
    assert report.limited_note_count == 1
    assert report.duplicate_alias_count == 1
    assert report.parked_for_review_count == 0
    assert reader.calls == 1
    assert [row["terminal_status"] for row in report.source_set["rows"]] == [
        "validated_note",
        "duplicate_alias",
        "limited_note",
    ]
    attempts = (
        tmp_path / "01_custody" / "read_attempts" / "coverage-run.jsonl"
    ).read_text()
    assert "duplicate_alias_of:ITEMA" in attempts
    assert "metadata_only" in attempts


def test_cloud_reader_is_never_called_without_explicit_consent(
    tmp_path: Path, sample_items
) -> None:
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


def test_fingerprint_rerun_and_same_run_resume_skip_reader(
    tmp_path: Path, sample_items
) -> None:
    client = FakeZotero(sample_items)
    first_reader = FakeReader()
    request = MapRequest(tmp_path, provider="ollama", model="fake-1", parallel=1)
    run_map(request, client=client, reader=first_reader, run_id="first")
    assert first_reader.calls == 2

    second_reader = FakeReader()
    second = run_map(
        request, client=FakeZotero(sample_items), reader=second_reader, run_id="second"
    )
    assert second.validated_note_count == 2
    assert second.reused_count == 2
    assert second_reader.calls == 0
    assert len(list((tmp_path / "02_source_memory" / "notes").glob("*.md"))) == 2

    resume_reader = FakeReader()
    resumed = resume_map(
        tmp_path, "first", client=FakeZotero(sample_items), reader=resume_reader
    )
    assert resumed.validated_note_count == 2
    assert resumed.reused_count == 0
    assert resume_reader.calls == 0


def test_processing_budget_change_reuses_current_schema_committed_note(
    tmp_path: Path, sample_items
) -> None:
    first_reader = FakeReader()
    first_request = MapRequest(
        tmp_path,
        provider="ollama",
        model="fake-1",
        parallel=1,
        processing=ProcessingPolicy(request_deadline_seconds=120),
    )
    run_map(
        first_request,
        client=FakeZotero(sample_items[:1]),
        reader=first_reader,
        run_id="budget-first",
    )
    assert first_reader.calls == 1
    note_path = next((tmp_path / "02_source_memory" / "notes").glob("*.md"))
    note_before = (note_path.read_bytes(), note_path.stat().st_mtime_ns)
    for fingerprint_path in (tmp_path / "11_state" / "fingerprints").glob("*.yml"):
        payload = yaml.safe_load(fingerprint_path.read_text())
        payload["engine_version"] = "0.3.0"
        payload["artifact_schema_version"] = "1.2"
        payload.pop("metadata_hash", None)
        fingerprint_path.write_text(yaml.safe_dump(payload, sort_keys=False))

    second_reader = FakeReader()
    second_request = MapRequest(
        tmp_path,
        provider="ollama",
        model="fake-1",
        parallel=1,
        processing=ProcessingPolicy(request_deadline_seconds=240),
    )
    second = run_map(
        second_request,
        client=FakeZotero(sample_items[:1]),
        reader=second_reader,
        run_id="budget-second",
    )

    assert second.validated_note_count == 1
    assert second.reused_count == 1
    assert second.items[0]["reason"] == "fingerprint_match"
    assert second_reader.calls == 0
    assert (note_path.read_bytes(), note_path.stat().st_mtime_ns) == note_before


def test_resume_revalidates_item_identity_and_content_fingerprint(
    tmp_path: Path, sample_items
) -> None:
    request = MapRequest(tmp_path, provider="ollama", model="fake-1", parallel=1)
    run_map(
        request,
        client=FakeZotero(sample_items),
        reader=FakeReader(),
        run_id="identity-run",
    )
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
    resumed = resume_map(
        tmp_path,
        "identity-run",
        client=FakeZotero([replacement, sample_items[1]]),
        reader=reader,
    )
    assert reader.calls == 0
    assert resumed.reused_count == 0
    assert [row["zotero_item_key"] for row in resumed.items] == ["ITEMA", "ITEMB"]
    assert resumed.source_set["zotero_item_keys"] == ["ITEMA", "ITEMB"]
    assert resumed.source_set["refresh_requires_new_run"] is True


def test_interrupted_run_reuses_direct_checkpoint_for_missing_note(
    tmp_path: Path, sample_items
) -> None:
    request = MapRequest(tmp_path, provider="ollama", model="fake-1", parallel=1)
    completed = run_map(
        request,
        client=FakeZotero(sample_items),
        reader=FakeReader(),
        run_id="interrupted",
    )
    missing = next(row for row in completed.items if row["zotero_item_key"] == "ITEMB")
    (tmp_path / missing["note_path"]).unlink()
    (tmp_path / "11_state" / "fingerprints" / f"{missing['fingerprint']}.yml").unlink()
    (tmp_path / "11_state" / "runs" / "interrupted" / "run_report.yml").unlink()

    reader = FakeReader()
    resumed = resume_map(
        tmp_path, "interrupted", client=FakeZotero(sample_items), reader=reader
    )
    assert resumed.status == "completed"
    assert resumed.inventory_count == resumed.terminal_count == 2
    assert resumed.validated_note_count == 2
    assert resumed.reused_count == 1
    assert reader.calls == 0


def test_untagged_source_can_commit_without_creating_cluster(
    tmp_path: Path, sample_items
) -> None:
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


def test_duplicate_work_identity_reuses_one_canonical_note(
    tmp_path: Path, sample_items
) -> None:
    collision = {
        **sample_items[0],
        "key": "OTHER",
        "data": {**sample_items[0]["data"], "key": "OTHER"},
    }
    report = run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1", parallel=2),
        client=FakeZotero([sample_items[0], collision]),
        reader=FakeReader(),
        run_id="collision",
    )
    assert report.inventory_count == report.terminal_count == 2
    assert report.validated_note_count == 1
    assert report.duplicate_alias_count == 1
    assert report.exhausted_count == 0
    names = sorted(
        path.name for path in (tmp_path / "02_source_memory" / "notes").glob("*.md")
    )
    assert len(names) == 1
    note = read_note(next((tmp_path / "02_source_memory" / "notes").glob("*.md")))
    assert note["frontmatter"]["canonical_zotero_key"] == "ITEMA"
    assert note["frontmatter"]["zotero_item_keys"] == ["ITEMA", "OTHER"]


def test_shared_container_doi_does_not_merge_distinct_chapters(
    tmp_path: Path, sample_items
) -> None:
    first = {
        **sample_items[0],
        "data": {
            **sample_items[0]["data"],
            "itemType": "bookSection",
            "title": "Chapter One",
            "DOI": "10.4324/9781003048404",
        },
    }
    second = {
        **first,
        "key": "OTHER",
        "data": {
            **first["data"],
            "key": "OTHER",
            "title": "Chapter Two",
        },
    }
    reader = FakeReader()

    report = run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1", parallel=2),
        client=FakeZotero([first, second]),
        reader=reader,
        run_id="shared-container-doi",
    )

    assert report.validated_note_count == 2
    assert report.duplicate_alias_count == 0
    assert reader.calls == 2
    assert len(list((tmp_path / "02_source_memory" / "notes").glob("*.md"))) == 2


def test_shared_container_doi_does_not_merge_incremental_chapter(
    tmp_path: Path, sample_items
) -> None:
    first = {
        **sample_items[0],
        "data": {
            **sample_items[0]["data"],
            "itemType": "bookSection",
            "title": "Chapter One",
            "DOI": "10.4324/9781003048404",
        },
    }
    second = {
        **first,
        "key": "OTHER",
        "data": {
            **first["data"],
            "key": "OTHER",
            "title": "Chapter Two",
        },
    }
    run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1"),
        client=FakeZotero([first]),
        reader=FakeReader(),
        run_id="first-chapter",
    )
    reader = FakeReader()

    report = run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1"),
        client=FakeZotero([second]),
        reader=reader,
        run_id="second-chapter",
    )

    assert report.validated_note_count == 1
    assert report.duplicate_alias_count == 0
    assert reader.calls == 1
    assert len(list((tmp_path / "02_source_memory" / "notes").glob("*.md"))) == 2


def test_explicit_same_work_relation_merges_duplicate_records(
    tmp_path: Path, sample_items
) -> None:
    relation = {
        "owl:sameAs": ["http://zotero.org/groups/123/items/CANONICAL"]
    }
    first = {
        **sample_items[0],
        "data": {
            **sample_items[0]["data"],
            "relations": relation,
        },
    }
    second = {
        **sample_items[0],
        "key": "OTHER",
        "data": {
            **sample_items[0]["data"],
            "key": "OTHER",
            "title": "A harmless metadata variant",
            "relations": relation,
        },
    }
    reader = FakeReader()

    report = run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1"),
        client=FakeZotero([first, second]),
        reader=reader,
        run_id="same-work-relation",
    )

    assert report.validated_note_count == 1
    assert report.duplicate_alias_count == 1
    assert reader.calls == 1


def test_unique_same_as_does_not_hide_duplicate_work_identity(
    tmp_path: Path, sample_items
) -> None:
    first = {
        **sample_items[0],
        "data": {
            **sample_items[0]["data"],
            "relations": {
                "owl:sameAs": ["https://example.org/record/first"]
            },
        },
    }
    second = {
        **sample_items[0],
        "key": "OTHER",
        "data": {**sample_items[0]["data"], "key": "OTHER"},
    }
    reader = FakeReader()

    report = run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1"),
        client=FakeZotero([first, second]),
        reader=reader,
        run_id="unique-same-as",
    )

    assert report.validated_note_count == 1
    assert report.duplicate_alias_count == 1
    assert reader.calls == 1


def test_alias_only_collection_source_set_includes_canonical_note(
    tmp_path: Path, sample_items
) -> None:
    canonical = run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1"),
        client=FakeZotero(sample_items[:1]),
        reader=FakeReader(),
        run_id="canonical",
    )
    alias = {
        **sample_items[0],
        "key": "OTHER",
        "data": {**sample_items[0]["data"], "key": "OTHER"},
    }
    reader = FakeReader()

    report = run_map(
        MapRequest(
            tmp_path,
            scope="collection",
            collection_key="ALIASES",
            provider="ollama",
            model="fake-1",
        ),
        client=FakeZotero([alias]),
        reader=reader,
        run_id="alias-only",
    )

    assert reader.calls == 0
    assert report.duplicate_alias_count == 1
    assert report.source_set["source_ids"] == [canonical.items[0]["source_id"]]
    assert len(report.source_set["rows"]) == 1


def test_question_is_only_a_projection_lens_but_metadata_and_reader_remain_in_fingerprint(
    tmp_path: Path, sample_items
) -> None:
    class QuestionReader(FakeReader):
        name = "actual-reader"
        model = "actual-v1"

        def read_source(self, text, metadata, question=None):
            self.calls += 1
            analysis = {
                key: f"{question} / {metadata['title']} / {key} / page 1"
                for key in __import__("conftest").SECTION_KEYS
            }
            analysis["plain_english_interpretation"] = (
                "Direction: The reported outcome changes in the direction stated by the source.\n"
                "Magnitude: The source's technical estimate is preserved without inventing a new scale.\n"
                "Reference point: The source's own comparison is used.\n"
                "Uncertainty: Only uncertainty reported by the source is retained.\n"
                "Practical meaning: This is the non-technical reading of the reported finding."
            )
            return analysis

    first_reader = QuestionReader()
    first = run_map(
        MapRequest(
            tmp_path, provider="ollama", model="requested-model", question="FIRST"
        ),
        client=FakeZotero(sample_items[:1]),
        reader=first_reader,
        run_id="question-first",
    )
    note_path = tmp_path / first.items[0]["note_path"]
    front = read_note(note_path)["frontmatter"]
    assert front["reader_provider"] == "actual-reader"
    assert front["reader_model"] == "actual-v1"

    changed = [json.loads(json.dumps(sample_items[0]))]
    changed[0]["data"]["title"] = "Changed Metadata Title"
    second_reader = QuestionReader()
    second = run_map(
        MapRequest(
            tmp_path, provider="ollama", model="requested-model", question="SECOND"
        ),
        client=FakeZotero(changed),
        reader=second_reader,
        run_id="question-second",
    )
    assert second.reused_count == 1
    assert second_reader.calls == 0
    text = (tmp_path / second.items[0]["note_path"]).read_text()
    assert "title: Changed Metadata Title" in text
    assert "# Changed Metadata Title" in text

    third_reader = QuestionReader()
    third = run_map(
        MapRequest(
            tmp_path,
            provider="ollama",
            model="requested-model",
            question="A DIFFERENT LENS",
        ),
        client=FakeZotero(changed),
        reader=third_reader,
        run_id="question-third",
    )
    assert third.reused_count == 1
    assert third_reader.calls == 0


def test_current_prompt_replaces_prompt_v1_note_instead_of_reusing_it(
    tmp_path: Path, sample_items
) -> None:
    first_reader = FakeReader()
    first = run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1", prompt_version="1"),
        client=FakeZotero(sample_items[:1]),
        reader=first_reader,
        run_id="prompt-v1",
    )
    assert first.validated_note_count == 1
    assert first_reader.calls == 1

    second_reader = FakeReader()
    second = run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1"),
        client=FakeZotero(sample_items[:1]),
        reader=second_reader,
        run_id="prompt-v2",
    )
    assert second.reused_count == 0
    assert second_reader.calls == 1
    note = tmp_path / second.items[0]["note_path"]
    frontmatter = read_note(note)["frontmatter"]
    _, body = parse_atomic_note(note.read_text())
    assert frontmatter["prompt_version"] == "11"
    assert "## Plain-English Interpretation" in body


def test_legacy_reader_without_plain_english_field_uses_disclosed_compatibility_fallback(
    tmp_path: Path, sample_items
) -> None:
    class LegacyReader(FakeReader):
        def read_source(self, text, metadata, question=None):
            self.calls += 1
            return {
                key: f"Legacy source-grounded {key}; see page 1."
                for key in __import__("conftest").SECTION_KEYS
                if key != "plain_english_interpretation"
            }

    report = run_map(
        MapRequest(tmp_path, provider="ollama", model="legacy-reader"),
        client=FakeZotero(sample_items[:1]),
        reader=LegacyReader(),
        run_id="legacy-reader-v2",
    )
    assert report.validated_note_count == 1
    note = tmp_path / report.items[0]["note_path"]
    assert (
        "this legacy reader did not provide a separate translation" in note.read_text()
    )


def test_long_document_uses_bounded_chunk_reader_route(
    tmp_path: Path, sample_items
) -> None:
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
    attempts = (
        tmp_path / "01_custody" / "read_attempts" / "long-source.jsonl"
    ).read_text()
    assert "fake-reader_hierarchical_text" in attempts


def test_partial_zotero_fulltext_is_not_treated_as_exhaustive(
    tmp_path: Path, sample_items
) -> None:
    class PartialZotero(FakeZotero):
        def fulltext(self, item_key):
            if item_key.endswith("PDF"):
                return {
                    "content": "Only one indexed page. " * 10,
                    "indexedPages": 1,
                    "totalPages": 100,
                }
            return None

    client = PartialZotero(sample_items[:1])
    reader = FakeReader()
    report = run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1"),
        client=client,
        reader=reader,
        run_id="partial-fulltext",
    )
    assert report.limited_note_count == 1
    assert report.exhausted_count == 0
    assert reader.calls == 0
    assert client.file_calls == 1
    attempts = (
        tmp_path / "01_custody" / "read_attempts" / "partial-fulltext.jsonl"
    ).read_text()
    assert "partial_or_unproven_indexed_pdf" in attempts


def test_synthetic_library_covers_readable_scanned_and_missing_sources(
    tmp_path: Path,
) -> None:
    items = json.loads(
        (Path(__file__).parent / "fixtures" / "library.json").read_text()
    )

    class ReleaseGateZotero(FakeZotero):
        def children(self, item_key):
            if item_key == "MISSING1":
                return []
            return [
                {
                    "key": item_key + "PDF",
                    "data": {
                        "key": item_key + "PDF",
                        "itemType": "attachment",
                        "contentType": "application/pdf",
                        "filename": item_key + ".pdf",
                    },
                }
            ]

        def fulltext(self, item_key):
            if item_key == "READABLE1PDF":
                return {
                    "content": "\f".join(
                        ["Readable inspected synthetic source. " * 12] * 10
                    ),
                    "contentType": "application/pdf",
                    "indexedPages": 10,
                    "totalPages": 10,
                }
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
    assert report.limited_note_count == 2
    assert report.exhausted_count == 0
    attempts = (
        tmp_path / "01_custody" / "read_attempts" / "synthetic-release-gate.jsonl"
    ).read_text()
    assert '"route": "pypdf_text"' in attempts
    assert "pdf_error" in attempts
    assert "metadata_only" in attempts


def test_workspace_map_remains_coherent_across_collection_runs(
    tmp_path: Path, sample_items
) -> None:
    request = MapRequest(tmp_path, provider="ollama", model="fake-1")
    run_map(
        request,
        client=FakeZotero(sample_items),
        reader=FakeReader(),
        run_id="workspace-one",
    )
    second = run_map(
        request,
        client=FakeZotero(sample_items[:1]),
        reader=FakeReader(),
        run_id="workspace-two",
    )
    assert second.cluster_map["clusters"] == []
    rebuilt = build_map(tmp_path, run_id="workspace-rebuild")
    assert rebuilt.metadata["cluster_map"]["clusters"] == []
    assert (
        list((tmp_path / "03_literature_synthesis" / "clusters").glob("Cluster - *.md"))
        == []
    )
    assert (
        list(
            (tmp_path / "03_literature_synthesis" / "gaps" / "candidates").glob(
                "Gap - *.md"
            )
        )
        == []
    )
    exported = export_to_obsidian(tmp_path, tmp_path / "vault", new_vault=True)
    assert exported.metadata["missing_wikilink_count"] == 0


def test_collection_build_clears_stale_cluster_projection_across_workspace(
    tmp_path: Path, sample_items
) -> None:
    report = run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1"),
        client=FakeZotero(sample_items),
        reader=FakeReader(),
        run_id="global-projection-source",
    )
    stale_path = tmp_path / report.items[1]["note_path"]
    update_note_frontmatter(
        stale_path,
        {
            "clusters": ["cluster-stale"],
            "cluster_links": ["[[Cluster - stale]]"],
        },
    )
    selected = {
        **dict(report.source_set),
        "source_ids": [report.items[0]["source_id"]],
        "note_ids": [report.items[0]["note_id"]],
        "rows": [dict(report.source_set["rows"][0])],
        "inventory_count": 1,
        "terminal_count": 1,
        "validated_note_count": 1,
        "limited_note_count": 0,
        "parked_for_review_count": 0,
        "partial_count": 0,
        "pending_count": 0,
    }

    build_map(
        tmp_path,
        run_id="global-projection-rebuild",
        source_set=selected,
    )

    frontmatter = read_note(stale_path)["frontmatter"]
    assert frontmatter["clusters"] == []
    assert frontmatter["cluster_links"] == []


def test_existing_custody_relations_feed_compatible_typed_note_links(
    tmp_path: Path, sample_items
) -> None:
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
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "relation_id",
                "source_id",
                "related_source_id",
                "relation_type",
                "route",
                "confidence",
                "created_at",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "relation_id": "rel-1",
                "source_id": report.items[0]["source_id"],
                "related_source_id": report.items[1]["source_id"],
                "relation_type": "closest_prior_work",
                "route": "fixture",
                "confidence": "high",
                "created_at": "2026-01-01",
            }
        )
    limited_path = tmp_path / report.items[1]["note_path"]
    limited_path.write_text(
        limited_path.read_text().replace(
            "note_status: analytical_atomic_note", "note_status: fulltext_available", 1
        )
    )
    build_map(tmp_path, run_id="custody-rebuild")
    compatibility = yaml.safe_load(
        (tmp_path / "02_source_memory" / "indexes" / "typed_note_links.yml").read_text()
    )
    assert compatibility["links"][0]["relation_type"] == "zotero_related"
    assert (
        compatibility["links"][0]["provenance"]
        == "01_custody/source_relation_registry.csv"
    )


def test_status_uses_run_snapshot_and_reports_missing_run(
    tmp_path: Path, sample_items
) -> None:
    run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1"),
        client=FakeZotero(sample_items),
        reader=FakeReader(),
        run_id="snapshot-run",
    )
    (tmp_path / "03_literature_synthesis" / "clusters" / "clusters.yml").write_text(
        "clusters: []\n"
    )
    assert get_status(tmp_path, "snapshot-run").counts["cluster_count"] == 0
    missing = get_status(tmp_path, "does-not-exist")
    assert missing.status == "blocked"
    assert missing.message == "run_not_found"


def test_selected_collection_key_and_limit_are_preserved(
    tmp_path: Path, sample_items
) -> None:
    client = FakeZotero(sample_items, selected_key="ACTUALSELECTED")
    report = run_map(
        MapRequest(
            tmp_path, scope="selected", provider="ollama", model="fake-1", limit=1
        ),
        client=client,
        reader=FakeReader(),
        run_id="selected-run",
    )
    assert client.inventory_calls == [
        ("collection", "ACTUALSELECTED"),
        ("library", None),
    ]
    assert report.inventory_count == 1
    assert report.source_set["zotero_collection_key"] == "ACTUALSELECTED"
    assert report.source_set["source_set_type"] == "zotero_collection"


def test_status_reports_terminal_and_literature_counts(
    tmp_path: Path, sample_items
) -> None:
    report = run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1"),
        client=FakeZotero(sample_items),
        reader=FakeReader(),
        run_id="status-run",
    )
    status = get_status(tmp_path, "status-run")
    assert status.status == "completed"
    assert status.counts["inventory_count"] == status.counts["terminal_count"] == 2
    assert status.counts["cluster_count"] == 0
    assert status.counts["gap_candidate_count"] == 0

    manifest = build_map(
        tmp_path, run_id="status-run", source_set=report.source_set, resume=True
    )
    rebuilt_status = get_status(tmp_path, "status-run")
    assert rebuilt_status.counts["inventory_count"] == 2
    assert rebuilt_status.counts["validated_note_count"] == 2
    assert rebuilt_status.counts["terminal_count"] == 2
    assert (
        manifest.metadata["literature_map"]["unclustered_count"]
        == rebuilt_status.counts["unclustered_count"]
    )


def test_build_map_reconstructs_progress_for_legacy_source_set_without_rows(
    tmp_path: Path, sample_items
) -> None:
    report = run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1"),
        client=FakeZotero(sample_items),
        reader=FakeReader(),
        run_id="legacy-source-run",
    )
    legacy_source_set = dict(report.source_set)
    legacy_source_set.pop("rows", None)

    build_map(
        tmp_path,
        run_id="legacy-build-run",
        source_set=legacy_source_set,
    )

    progress = yaml.safe_load(
        (
            tmp_path
            / "11_state"
            / "runs"
            / "legacy-build-run"
            / "progress.yml"
        ).read_text()
    )
    assert progress["status"] == "completed"
    assert progress["inventory_count"] == 2
    assert progress["validated_note_count"] == 2
    assert progress["terminal_count"] == 2


def test_resumed_progress_ignores_stale_source_counts_inside_literature(
    tmp_path: Path,
) -> None:
    path = tmp_path / "progress.yml"
    items = [
        {"key": "A", "terminal_status": "validated_note"},
        {"key": "B", "terminal_status": "limited_note"},
    ]
    _RunProgress(path, "progress-run", items, resume=False)
    payload = yaml.safe_load(path.read_text())
    payload["literature"].update(
        inventory_count=75,
        validated_note_count=65,
        limited_note_count=8,
        exhausted_count=2,
        terminal_count=75,
    )
    path.write_text(yaml.safe_dump(payload, sort_keys=False))

    _RunProgress(path, "progress-run", items, resume=True)

    resumed = yaml.safe_load(path.read_text())
    assert resumed["inventory_count"] == 2
    assert resumed["validated_note_count"] == 1
    assert resumed["limited_note_count"] == 1
    assert resumed["parked_for_review_count"] == 0
    assert resumed["terminal_count"] == 2
    assert "validated_note_count" not in resumed["literature"]


def test_reporting_stage_preserves_75_item_progress_and_partial_is_not_terminal(
    tmp_path: Path,
) -> None:
    source_set = {
        "inventory_count": 75,
        "validated_note_count": 60,
        "limited_note_count": 10,
        "exhausted_count": 2,
        "partial_count": 1,
        "pending_count": 2,
        "zotero_item_keys": [f"ITEM-{index:02d}" for index in range(75)],
    }
    progress = _RunProgress(
        tmp_path / "progress.yml",
        "progress-75",
        _progress_items_from_source_set(source_set),
        resume=False,
    )

    progress.set_stage("reporting")
    progress.finish("partial")

    payload = yaml.safe_load((tmp_path / "progress.yml").read_text())
    assert payload["inventory_count"] == 75
    assert payload["validated_note_count"] == 60
    assert payload["limited_note_count"] == 10
    assert payload["parked_for_review_count"] == 2
    assert payload["partial_count"] == 1
    assert payload["pending_count"] == 2
    assert payload["terminal_count"] == 72
    assert payload["inventory_count"] == (
        payload["terminal_count"]
        + payload["partial_count"]
        + payload["pending_count"]
    )
    assert payload["status"] == "partial"


def test_progress_reconstruction_rejects_malformed_source_set_counts() -> None:
    with pytest.raises(ValueError, match="do not reconcile"):
        _progress_items_from_source_set(
            {
                "inventory_count": 75,
                "validated_note_count": 60,
                "limited_note_count": 10,
                "exhausted_count": 2,
                "partial_count": 1,
                "pending_count": 1,
            }
        )


def test_large_document_checkpoints_and_resume_avoid_repeated_calls(
    tmp_path: Path, sample_items
) -> None:
    class LongTextZotero(FakeZotero):
        def fulltext(self, item_key):
            if item_key.endswith("PDF"):
                return {
                    "content": "Grounded paragraph with page evidence. " * 40,
                    "contentType": "text/plain",
                }
            return None

    class HierarchicalReader(FakeReader):
        def __init__(self):
            super().__init__()
            self.chunk_ids: list[str] = []
            self.synthesis_calls = 0

        def summarize_chunk(
            self, text, metadata, question=None, *, chunk_id="", locator="", **kwargs
        ):
            self.calls += 1
            self.chunk_ids.append(chunk_id)
            return {
                "summary": f"Summary {chunk_id}",
                "claims_and_findings": "Grounded finding",
                "methods_and_data": "Reported method",
                "limitations": "Reported limitation",
                "locators": locator,
            }

        def synthesize_document(self, chunk_memos, metadata, question=None, **kwargs):
            self.calls += 1
            self.synthesis_calls += 1
            return {
                key: f"Synthesized {key}; see page 1."
                for key in __import__("conftest").SECTION_KEYS
            }

    policy = ProcessingPolicy(
        direct_read_char_limit=100,
        chunk_char_limit=300,
        max_total_chunks=64,
        max_calls_per_document_run=2,
        request_deadline_seconds=30,
        document_deadline_seconds=120,
    )
    request = MapRequest(
        tmp_path, provider="ollama", model="fake-1", parallel=1, processing=policy
    )
    reader = HierarchicalReader()
    client = LongTextZotero(sample_items[:1])

    first = run_map(request, client=client, reader=reader, run_id="checkpoint-run")
    assert first.status == "partial"
    assert first.partial_count == 1
    first_chunk_ids = list(reader.chunk_ids)
    assert first_chunk_ids == ["chunk-0001", "chunk-0002"]

    second = resume_map(tmp_path, "checkpoint-run", client=client, reader=reader)
    assert second.status == "partial"
    assert reader.chunk_ids[:2] == first_chunk_ids
    assert reader.chunk_ids.count("chunk-0001") == 1
    assert reader.chunk_ids.count("chunk-0002") == 1

    completed = second
    for _ in range(4):
        if completed.status != "partial":
            break
        completed = resume_map(tmp_path, "checkpoint-run", client=client, reader=reader)
    assert completed.status == "completed"
    assert completed.validated_note_count == 1
    assert reader.synthesis_calls == 1
    assert len(reader.chunk_ids) == len(set(reader.chunk_ids))


def test_completed_item_is_committed_while_another_worker_is_slow(
    tmp_path: Path, sample_items
) -> None:
    slow_started = threading.Event()
    release_slow = threading.Event()
    result: list[object] = []

    class SlowReader(FakeReader):
        def read_source(self, text, metadata, question=None):
            if metadata.get("title") == "Institutions in Practice":
                slow_started.set()
                assert release_slow.wait(5)
            return super().read_source(text, metadata, question)

    def execute() -> None:
        result.append(
            run_map(
                MapRequest(tmp_path, provider="ollama", model="fake-1", parallel=2),
                client=FakeZotero(sample_items),
                reader=SlowReader(),
                run_id="visible-progress",
            )
        )

    thread = threading.Thread(target=execute, daemon=True)
    thread.start()
    assert slow_started.wait(2)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not list(
        (tmp_path / "02_source_memory" / "notes").glob("*.md")
    ):
        time.sleep(0.01)
    assert len(list((tmp_path / "02_source_memory" / "notes").glob("*.md"))) == 1
    live = get_status(tmp_path, "visible-progress")
    while time.monotonic() < deadline and live.counts["validated_note_count"] != 1:
        time.sleep(0.01)
        live = get_status(tmp_path, "visible-progress")
    assert live.status == "running"
    assert live.counts["validated_note_count"] == 1
    assert live.counts["pending_count"] == 1
    release_slow.set()
    thread.join(5)
    assert not thread.is_alive()
    assert result and result[0].validated_note_count == 2


def test_hard_chunk_limit_creates_fulltext_available_limited_note(
    tmp_path: Path, sample_items
) -> None:
    class HugeTextZotero(FakeZotero):
        def fulltext(self, item_key):
            if item_key.endswith("PDF"):
                return {
                    "content": "Large source paragraph. " * 100,
                    "contentType": "text/plain",
                }
            return None

    policy = ProcessingPolicy(
        direct_read_char_limit=100,
        chunk_char_limit=200,
        max_total_chunks=2,
        request_deadline_seconds=30,
        document_deadline_seconds=120,
    )
    report = run_map(
        MapRequest(tmp_path, provider="ollama", model="fake-1", processing=policy),
        client=HugeTextZotero(sample_items[:1]),
        reader=FakeReader(),
        run_id="hard-limit",
    )
    assert report.limited_note_count == 1
    note = tmp_path / report.items[0]["note_path"]
    frontmatter = read_note(note)["frontmatter"]
    _, body = parse_atomic_note(note.read_text())
    assert frontmatter["note_status"] == "fulltext_available"
    assert frontmatter["source_scope"] == "full_document"
    assert "Processing Status" in body


def test_million_context_reader_handles_tested_book_size_in_one_call(
    tmp_path: Path, sample_items
) -> None:
    class BookZotero(FakeZotero):
        def fulltext(self, item_key):
            if item_key.endswith("PDF"):
                return {
                    "content": ("Grounded source evidence. " * 50_000)[:1_090_074],
                    "contentType": "text/plain",
                }
            return None

    class MillionContextReader(FakeReader):
        context_window_tokens = 1_000_000

    reader = MillionContextReader()
    report = run_map(
        MapRequest(
            tmp_path, provider="deepseek", model="deepseek-v4-flash", allow_cloud=True
        ),
        client=BookZotero(sample_items[:1]),
        reader=reader,
        run_id="million-context-book",
    )
    assert report.validated_note_count == 1
    assert reader.calls == 1
    assert (
        tmp_path
        / "11_state"
        / "runs"
        / "million-context-book"
        / "items"
        / "ITEMA"
        / "direct.yml"
    ).exists()
