from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path

import pytest
import yaml

from auto_zettelkasten.mapping_text import load_mapping_inputs, prepare_embedding_spans
from auto_zettelkasten.models import EvidenceProfile
from auto_zettelkasten.notes import NOTE_METADATA_SCHEMA_VERSION, public_note_frontmatter
from auto_zettelkasten.profiles import save_profile


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else yaml.safe_dump(value), encoding="utf-8")


def _note(root: Path, sid="source-a", nid="note-a", body="## Thesis\nA synthetic argument.\n"):
    path = root / "02_source_memory/notes" / f"{nid}.md"
    _write(path, "---\n" + yaml.safe_dump({"source_id": sid, "note_id": nid, "title": "Synthetic source"}) + "---\n" + body)
    return {"source_id": sid, "note_id": nid, "note_path": str(path.relative_to(root)), "title": "Synthetic source", "zotero_item_key": "ITEM0001"}


def test_bibliography_filter_retains_later_chapters_and_exact_citable_original():
    text = "Chapter 1\r\nFirst argument.\r\nReferences\r\nA. Example 2020.\r\nChapter 2\r\nA different argument.\r\n"
    result = prepare_embedding_spans(text)
    clean = "".join(row["text"] for row in result["spans"])
    assert "First argument" in clean and "A different argument" in clean
    assert "A. Example" not in clean
    assert "A. Example" in text  # the source/citation text was not rewritten
    assert all(text[row["start"]:row["end"]] == row["text"] for row in result["spans"])
    assert result["coverage"]["excluded_by_kind"]["bibliography"] > 0
    assert not result["coverage"]["whole_document_coverage_certified"]


def test_numbered_reference_entries_cannot_be_mistaken_for_new_body_sections():
    text = "1 Introduction\nSubstantive argument.\nReferences\n1. Example A (2024). A study.\n2. Example B (2025). Another study.\n2 Methods\nNew chapter methods.\n"
    clean = "".join(row["text"] for row in prepare_embedding_spans(text)["spans"])
    assert "Example A" not in clean and "Example B" not in clean
    assert "Substantive argument" in clean and "New chapter methods" in clean


def test_explicit_nonbody_sections_do_not_remove_next_substantive_body():
    text = "Contents\nChapter 1 .... 7\nChapter 1\nActual analysis.\nAcknowledgments\nThanks to helpers.\n## Discussion\nA substantive discussion.\nFunding\nGrant ABC.\nConclusion\nLast finding.\n"
    clean = "".join(row["text"] for row in prepare_embedding_spans(text)["spans"])
    assert all(value in clean for value in ("Actual analysis", "A substantive discussion", "Last finding"))
    assert all(value not in clean for value in ("....", "Thanks to helpers", "Grant ABC"))


def test_unowned_preface_is_preserved_but_not_attributed_to_an_editor():
    text = "Preface\nA meaningful account of the method.\nChapter 1\nA chapter by a different author.\n"
    result = prepare_embedding_spans(text)
    assert result["spans"][0]["section_kind"] == "authored_preface"
    assert all(row["owner"] == "" for row in result["spans"])
    assert result["coverage"]["ownership_status"] == "unresolved"
    end = text.index("Chapter 1")
    owned = prepare_embedding_spans(text, [{"start": 0, "end": end, "owner": "Preface Author"}])
    assert owned["spans"][0]["owner"] == "Preface Author"
    assert owned["spans"][-1]["owner"] == ""


def test_explicit_chapter_ownership_splits_spans_and_excludes_owned_reference_list():
    text = "First author's mechanism. Second author's finding. Reference list."
    cut, reference = text.index("Second"), text.index("Reference")
    spans = [{"start": 0, "end": cut, "owner": "Chapter author A", "section_kind": "body"},
             {"start": cut, "end": reference, "owner": "Chapter author B", "section_kind": "body"},
             {"start": reference, "end": len(text), "owner": "Chapter author B", "section_kind": "bibliography"}]
    before = deepcopy(spans)
    result = prepare_embedding_spans(text, spans)
    assert [row["owner"] for row in result["spans"]] == ["Chapter author A", "Chapter author B"]
    assert result["coverage"]["ownership_status"] == "explicit"
    assert "Reference list" not in "".join(row["text"] for row in result["spans"])
    assert spans == before


@pytest.mark.parametrize("spans", [
    [{"start": -1, "end": 5, "owner": "A"}],
    [{"start": 0, "end": 30, "owner": "A"}],
    [{"start": True, "end": 5, "owner": "A"}],
    [{"start": 0, "end": 5, "owner": "A", "text": "wrong"}],
    [{"start": 0, "end": 5, "owner": "A"}, {"start": 4, "end": 8, "owner": "B"}],
])
def test_bad_ownership_cannot_silently_reattribute_text(spans):
    with pytest.raises(ValueError):
        prepare_embedding_spans("A synthetic text.", spans)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("bom", ["", "\ufeff"])
def test_note_yaml_is_not_semantic_content_and_unmarked_body_stays_unresolved(newline, bom):
    text = bom + "---\nsource_id: s\nnote_id: n\n---\nUnmarked substantive text.\n".replace("\n", newline)
    result = prepare_embedding_spans(text)
    assert "source_id" not in "".join(row["text"] for row in result["spans"])
    assert "Unmarked substantive text" in result["spans"][0]["text"]
    assert result["coverage"]["unclassified_characters"] > 0
    assert all(text[row["start"]:row["end"]] == row["text"] for row in result["spans"])
    assert result["coverage"]["excluded_by_kind"]["frontmatter"] == text.index("Unmarked")


def test_managed_graph_navigation_is_not_reembedded_as_a_source_claim():
    text = "## Thesis\nA synthetic finding.\n<!-- auto-zettelkasten:graph:start -->\n[[Artificial hub]]\n<!-- auto-zettelkasten:graph:end -->\n## Limitations\nA bounded claim.\n"
    result = prepare_embedding_spans(text)
    clean = "".join(row["text"] for row in result["spans"])
    assert "Artificial hub" not in clean
    assert "A synthetic finding" in clean and "A bounded claim" in clean
    assert result["coverage"]["excluded_by_kind"]["generated_navigation"] > 0


def test_note_fallback_is_complete_note_only_and_input_changes_are_visible(tmp_path):
    row = _note(tmp_path)
    before = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    result = load_mapping_inputs(tmp_path, [row])
    source = result["text_sources"]["source-a"]
    assert source["source_scope"] == "note_only"
    assert source["coverage"]["reason"].endswith("complete_note_only")
    assert source["text"] == (tmp_path / row["note_path"]).read_text()
    assert result["projection_rows"][0]["projection_hash"] == _sha(source["text"])
    assert before == {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    old_digest = source["content_sha256"]
    with (tmp_path / row["note_path"]).open("a") as output:
        output.write("Additional argument.\n")
    assert load_mapping_inputs(tmp_path, [row])["text_sources"]["source-a"]["content_sha256"] != old_digest


def test_existing_frozen_source_cache_is_hash_bound_and_raw_references_survive(tmp_path):
    row = _note(tmp_path)
    custody_hash = "a" * 64
    _write(tmp_path / "11_state/note_metadata/note-a.yml", {"frontmatter": {"source_id": "source-a", "note_id": "note-a", "zotero_item_key": "ITEM0001", "inspected_content_hash": custody_hash}})
    text = "Chapter 1\nAn authored argument.\nReferences\nExample 2024.\n"
    checkpoint = tmp_path / "11_state/runs/synthetic-run/items/ITEM0001"
    _write(checkpoint / "source.txt", text)
    _write(checkpoint / "frozen_content.yml", {"checkpoint_version": "1", "content_hash": custody_hash, "text_hash": _sha(text), "source_scope": "full_document"})
    before = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    result = load_mapping_inputs(tmp_path, [row])
    source = result["text_sources"]["source-a"]
    assert source["text_origin"] == "frozen_content"
    assert source["source_scope"] == "full_document"
    assert source["text"] == text and "Example 2024" in source["text"]
    assert all(span["owner"] == "" for span in prepare_embedding_spans(source["text"])["spans"])
    assert before == {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    _write(checkpoint / "source.txt", text + "tampering")
    with pytest.raises(ValueError, match="integrity"):
        load_mapping_inputs(tmp_path, [row])


def test_wrong_source_hash_cache_is_not_reused_or_labeled_full_document(tmp_path):
    row = _note(tmp_path)
    _write(tmp_path / "11_state/note_metadata/note-a.yml", {"frontmatter": {"source_id": "source-a", "note_id": "note-a", "inspected_content_hash": "b" * 64}})
    checkpoint = tmp_path / "11_state/runs/synthetic-run/items/ITEM0001"
    _write(checkpoint / "source.txt", "Unrelated source text")
    _write(checkpoint / "frozen_content.yml", {"checkpoint_version": "1", "content_hash": "a" * 64, "text_hash": _sha("Unrelated source text"), "source_scope": "full_document"})
    assert load_mapping_inputs(tmp_path, [row])["text_sources"]["source-a"]["source_scope"] == "note_only"


def test_empty_and_missing_explicit_selections_do_not_expand_scope(tmp_path):
    first, second = _note(tmp_path), _note(tmp_path, "source-b", "note-b")
    assert load_mapping_inputs(tmp_path, [first, second], source_ids=[])["text_sources"] == {}
    result = load_mapping_inputs(tmp_path, [first, second], source_ids=["source-b", "missing"])
    assert list(result["text_sources"]) == ["source-b"]
    assert result["coverage"]["missing_source_ids"] == ["missing"]
    assert load_mapping_inputs(tmp_path, [])["text_sources"] == {}


def test_source_note_identity_and_confined_paths_fail_closed(tmp_path):
    row = _note(tmp_path)
    wrong = {**row, "source_id": "other"}
    with pytest.raises(ValueError, match="identity"):
        load_mapping_inputs(tmp_path, [wrong])
    with pytest.raises(ValueError, match="workspace"):
        load_mapping_inputs(tmp_path, [{**row, "note_path": "../outside.md"}])
    alias = tmp_path / "alias.md"
    alias.symlink_to(tmp_path / row["note_path"])
    with pytest.raises(ValueError, match="workspace"):
        load_mapping_inputs(tmp_path, [{**row, "note_path": "alias.md"}])


def test_no_source_identity_is_fabricated_from_a_title(tmp_path):
    with pytest.raises(ValueError, match="canonical"):
        load_mapping_inputs(tmp_path, [{"title": "Synthetic source", "note_id": "note-a"}])


def test_canonical_profile_projection_retains_semantic_hash_and_records_actual_file_hash(tmp_path):
    row = _note(tmp_path)
    profile = EvidenceProfile(source_id="source-a", note_id="note-a", note_hash="semantic-hash", concepts=["synthetic mechanism"])
    profile_path = save_profile(tmp_path / "02_source_memory/profiles", profile)
    _write(tmp_path / "02_source_memory/indexes/source_catalogue.yml", {"sources": [{"source_id": "source-a", "note_id": "note-a", "title": "Synthetic source", "zotero_key": "ITEM0001", "identity": {"zotero_item_keys": ["ITEM0001", "ALIAS001"]}}]})
    _write(tmp_path / "11_state/note_metadata/note-a.yml", {"note_path": row["note_path"], "frontmatter": {"source_id": "source-a", "note_id": "note-a"}})
    result = load_mapping_inputs(tmp_path)
    projected = result["projection_rows"][0]
    assert projected["note_hash"] == "semantic-hash"
    assert projected["projection_hash"] == _sha((tmp_path / row["note_path"]).read_text())
    assert projected["profile_file_sha256"] == _sha(profile_path.read_text())
    assert projected["profile_path"] == str(profile_path.relative_to(tmp_path))
    assert projected["zotero_item_keys"] == ["ALIAS001", "ITEM0001"]


def test_catalogue_sources_missing_profiles_or_text_are_reported_not_silently_sampled(tmp_path):
    _write(tmp_path / "02_source_memory/indexes/source_catalogue.yml", {"sources": [{"source_id": "source-missing", "note_id": "note-missing"}]})
    result = load_mapping_inputs(tmp_path)
    assert result["coverage"]["selected_source_count"] == 1
    assert result["coverage"]["loaded_source_count"] == 0
    assert result["coverage"]["missing_source_ids"] == ["source-missing"]


def test_recovery_cache_preserves_declared_partial_coverage(tmp_path):
    row = _note(tmp_path)
    custody_hash, text = "b" * 64, "Recovered partial pages only.\n"
    _write(tmp_path / "11_state/note_metadata/note-a.yml", {"frontmatter": {"source_id": "source-a", "note_id": "note-a", "inspected_content_hash": custody_hash}})
    folder = tmp_path / "11_state/pdf_local_recovery" / custody_hash
    _write(folder / "source.txt", text)
    _write(folder / "manifest.yml", {"cache_version": "2", "custody_sha256": custody_hash, "text_sha256": _sha(text), "source_adequacy": {"source_scope": "partial_document", "coverage_gate": "limited", "reason": "unresolved_pages"}})
    source = load_mapping_inputs(tmp_path, [row])["text_sources"]["source-a"]
    assert source["source_scope"] == "partial_document"
    assert source["coverage"]["source_coverage"]["reason"] == "unresolved_pages"
    assert source["text"] == text


def _public_note(root: Path):
    row = _note(root)
    front = {"source_id": row["source_id"], "note_id": row["note_id"], "title": "Readable source",
             "note_status": "analytical_atomic_note", "source_scope": "full_document"}
    path = root / row["note_path"]
    _write(path, "---\n" + yaml.safe_dump(public_note_frontmatter(front)) + "---\n## Thesis\nA bounded synthetic claim.\n")
    metadata = {"metadata_schema_version": NOTE_METADATA_SCHEMA_VERSION, "note_path": row["note_path"], "frontmatter": front}
    _write(root / "11_state/note_metadata/note-a.yml", metadata)
    return row, metadata


def test_current_readable_public_note_restores_identity_only_from_bound_canonical_sidecar(tmp_path):
    row, _ = _public_note(tmp_path)
    raw = (tmp_path / row["note_path"]).read_text()
    assert "source_id:" not in raw
    result = load_mapping_inputs(tmp_path, [row])
    assert result["text_sources"]["source-a"]["text"] == raw
    assert result["projection_rows"][0]["note_id"] == row["note_id"]
    metadata_path = tmp_path / "11_state/note_metadata/note-a.yml"
    assert result["projection_rows"][0]["metadata_file_sha256"] == _sha(metadata_path.read_text())
    assert result["projection_rows"][0]["metadata_path"] == str(metadata_path.relative_to(tmp_path))


@pytest.mark.parametrize("change", ["missing_sidecar", "wrong_schema", "missing_bound_path", "rerouted_path", "conflicting_source", "conflicting_note", "symlink_sidecar"])
def test_missing_public_identity_cannot_be_restored_by_unbound_or_conflicting_sidecar(tmp_path, change):
    row, metadata = _public_note(tmp_path)
    path = tmp_path / "11_state/note_metadata/note-a.yml"
    if change == "missing_sidecar":
        path.unlink()
    elif change == "wrong_schema":
        _write(path, {**metadata, "metadata_schema_version": "unknown"})
    elif change == "missing_bound_path":
        _write(path, {key: value for key, value in metadata.items() if key != "note_path"})
    elif change == "rerouted_path":
        _note(tmp_path, "other", "other")
        _write(path, {**metadata, "note_path": "02_source_memory/notes/other.md"})
    elif change in {"conflicting_source", "conflicting_note"}:
        raw = (tmp_path / row["note_path"]).read_text()
        key = "source_id" if change == "conflicting_source" else "note_id"
        raw = raw.replace("---\n", f"---\n{key}: conflicting\n", 1) if key == "source_id" else raw.replace("note_id: note-a", "note_id: conflicting")
        _write(tmp_path / row["note_path"], raw)
    elif change == "symlink_sidecar":
        other = path.with_name("other.yml")
        _write(other, metadata)
        path.unlink()
        path.symlink_to(other)
    with pytest.raises(ValueError):
        load_mapping_inputs(tmp_path, [row])


@pytest.mark.parametrize("target_kind", ["note", "metadata"])
def test_same_size_edits_during_text_or_yaml_read_cannot_become_a_new_input_baseline(tmp_path, monkeypatch, target_kind):
    row, _ = _public_note(tmp_path)
    target = tmp_path / (row["note_path"] if target_kind == "note" else "11_state/note_metadata/note-a.yml")
    original_open = Path.open
    changed = False

    class MutatingRead:
        def __init__(self, source):
            self.source = source

        def __enter__(self):
            self.source.__enter__()
            return self

        def __exit__(self, *args):
            return self.source.__exit__(*args)

        def read(self, *args):
            nonlocal changed
            raw = self.source.read(*args)
            if not changed:
                changed = True
                # Same byte count: size-only accounting would accept stale
                # bytes, and an after-read baseline would hide this mutation.
                replacement = raw.replace(b"Readable", b"Replaced", 1)
                assert len(replacement) == len(raw) and replacement != raw
                with original_open(target, "wb") as output:
                    output.write(replacement)
            return raw

    def open_with_one_mutation(path, *args, **kwargs):
        source = original_open(path, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        return MutatingRead(source) if path == target and mode == "rb" else source

    monkeypatch.setattr(Path, "open", open_with_one_mutation)
    with pytest.raises(ValueError, match="changed during read"):
        load_mapping_inputs(tmp_path, [row])
    assert changed


def test_input_stamps_return_first_reserved_identity_for_commit_recheck(tmp_path):
    row, _ = _public_note(tmp_path)
    result = load_mapping_inputs(tmp_path, [row])
    assert set(result["input_stamps"]) == set(result["input_paths"])
    for value, stamp in result["input_stamps"].items():
        stat = Path(value).stat()
        assert stamp == [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
