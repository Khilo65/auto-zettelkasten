"""Synthetic tests for optional portable links and protected effective notes."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from auto_zettelkasten import markdown_link_export as export
from auto_zettelkasten.documents import DocumentService
from auto_zettelkasten.notes import GRAPH_END_MARKER, GRAPH_START_MARKER, RELATED_END_MARKER, RELATED_START_MARKER, semantic_note_hash


class SnapshotDocuments:
    def __init__(self, root, notes, edges):
        self.root = root
        self.notes = {path: {"path": path, "text": text, "revision": hashlib.sha256(text.encode()).hexdigest()}
                      for path, text in notes.items()}
        self.edges = edges
        self.semantic = {"status": "current", "identity_digest": "a" * 64,
                         "note_revisions": {path: row["revision"] for path, row in self.notes.items()},
                         "coverage": {"stale": 0, "unavailable": 0, "unresolved": 0}}

    def workspace_path(self, workspace_id):
        assert workspace_id == "workspace"
        return str(self.root)

    def index(self, workspace_id):
        self.workspace_path(workspace_id)
        return {"documents": deepcopy(list(self.notes.values())), "relations": deepcopy(self.edges),
                "truncated": False, "index_coverage": {"status": "complete"},
                "semantic_map": deepcopy(self.semantic), "warnings": ["Typed relations unavailable."]}

    def read(self, workspace_id, path):
        self.workspace_path(workspace_id)
        return deepcopy(self.notes[path])


def edge(source, target, score=.8):
    return {"source": source, "target": target, "score": score, "type": "semantic_similarity",
            "derived": True, "accepted": False, "evidence_backed": False, "decision_status": "suggested"}


@pytest.fixture
def sample(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    notes = {"Notes/A.md": "# A\n\nHuman sentence and [[B]] stay intact.  \n",
             "Notes/B.md": "# B\n\nA substantive finding.\n", "Notes/C.md": "# C\n\nCounterevidence.\n"}
    for path, text in notes.items():
        file = root / path
        file.parent.mkdir(exist_ok=True)
        file.write_text(text)
    service = SnapshotDocuments(root, notes, [edge("Notes/A.md", "Notes/B.md", .9),
                                            edge("Notes/B.md", "Notes/A.md", .9),
                                            edge("Notes/A.md", "Notes/C.md", .7)])
    destination = tmp_path / "linked"
    return root, destination, service


def run(sample, **kwargs):
    root, destination, service = sample
    return export.export_markdown_links(root, destination, documents=service, workspace_id="workspace", **kwargs)


def test_preview_is_inert_and_apply_is_deterministic_idempotent(sample):
    root, destination, _ = sample
    before = {str(p): p.read_bytes() for p in root.rglob("*.md")}
    preview = run(sample)
    assert preview == run(sample)
    assert preview["status"] == "preview" and preview["note_count"] == 3 and preview["edge_count"] == 2
    assert not destination.exists()
    result = run(sample, apply=True, preview_identity=preview["preview_identity"])
    assert result["status"] == "exported" and result["provider_calls"] == 0 and result["jev_enabled"] is False
    text = (destination / "Notes/A.md").read_text()
    assert text.startswith(before[str(root / "Notes/A.md")].decode())
    assert "- [[Notes/B|B]]" in text and "- [[Notes/C|C]]" in text
    assert "- [[Notes/A|A]]" in (destination / "Notes/B.md").read_text()
    assert "Semantic similarity, not a claim of agreement." in text
    assert json.loads((destination / ".auto-zettelkasten/related-export.json").read_text())["automatic_link_origin"] == "local_embedding_similarity"
    unchanged = {str(p): p.read_bytes() for p in destination.rglob("*") if p.is_file()}
    assert run(sample, apply=True, preview_identity=preview["preview_identity"])["status"] == "unchanged"
    assert {str(p): p.read_bytes() for p in destination.rglob("*") if p.is_file()} == unchanged
    assert {str(p): p.read_bytes() for p in root.rglob("*.md")} == before


def test_export_never_overwrites_edited_or_unrelated_files(sample):
    _, destination, _ = sample
    destination.mkdir()
    (destination / "Human.md").write_text("User-owned work")
    with pytest.raises(ValueError, match="existing files"):
        run(sample, apply=True, preview_identity="irrelevant")
    assert (destination / "Human.md").read_text() == "User-owned work"


def test_changed_export_is_preserved(sample):
    _, destination, _ = sample
    preview = run(sample)
    run(sample, apply=True, preview_identity=preview["preview_identity"])
    file = destination / "Notes/A.md"
    file.write_text(file.read_text() + "\nNew personal insight.\n")
    with pytest.raises(ValueError, match="edited or differs"):
        run(sample)
    assert file.read_text().endswith("New personal insight.\n")


def test_degree_cap_is_per_endpoint_and_not_all_pairs(sample):
    root, destination, _ = sample
    paths = [f"{i}.md" for i in range(5)]
    service = SnapshotDocuments(root, {path: path for path in paths},
        [edge("0.md", path, .99 - i / 10) for i, path in enumerate(paths[1:])]
        + [edge("1.md", "2.md", .5), edge("3.md", "4.md", .4)])
    preview = export.export_markdown_links(root, destination, documents=service, workspace_id="workspace", degree_cap=2)
    export.export_markdown_links(root, destination, documents=service, workspace_id="workspace", degree_cap=2,
                                 apply=True, preview_identity=preview["preview_identity"])
    manifest = json.loads((destination / ".auto-zettelkasten/related-export.json").read_text())
    degrees = {path: 0 for path in paths}
    for row in manifest["links"]:
        degrees[row["source"]] += 1
        degrees[row["target"]] += 1
    assert max(degrees.values()) == 2
    assert preview["candidate_count"] == 6 and preview["edge_count"] == 4
    assert {tuple(sorted((r["source"], r["target"]))) for r in manifest["links"]} <= {
        tuple(sorted((r["source"], r["target"]))) for r in service.edges}


@pytest.mark.parametrize("value", [None, [], True, "", "relative", "../escape"])
def test_invalid_destinations_fail_before_writes(sample, value):
    root, destination, service = sample
    with pytest.raises(ValueError, match="destination"):
        export.export_markdown_links(root, value, documents=service, workspace_id="workspace")
    assert not destination.exists()


def test_destination_cannot_overlap_source_or_override_storage(sample, tmp_path):
    root, _, service = sample
    for destination in (root, root / "export", root.parent):
        with pytest.raises(ValueError, match="separate"):
            export.export_markdown_links(root, destination, documents=service, workspace_id="workspace")
    overrides = tmp_path / "overrides"
    service._overrides = SimpleNamespace(root=overrides)
    with pytest.raises(ValueError, match="separate"):
        export.export_markdown_links(root, overrides / "export", documents=service, workspace_id="workspace")


def test_symlinked_destination_is_rejected(sample, tmp_path):
    root, _, service = sample
    actual = tmp_path / "real"
    actual.mkdir()
    link = tmp_path / "link"
    link.symlink_to(actual, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        export.export_markdown_links(root, link / "export", documents=service, workspace_id="workspace")
    assert not (actual / "export").exists()


@pytest.mark.parametrize("change", ["note", "score", "cap"])
def test_exact_preview_identity_is_required(sample, change):
    _, destination, service = sample
    preview = run(sample)
    kwargs = {}
    if change == "note":
        service.notes["Notes/C.md"]["text"] += "Edited prose."
    elif change == "score":
        service.edges[0]["score"] = .95
    else:
        kwargs["degree_cap"] = 4
    with pytest.raises(ValueError, match="exact current preview"):
        run(sample, apply=True, preview_identity=preview["preview_identity"], **kwargs)
    assert not destination.exists()


def test_source_recheck_discards_staging_on_concurrent_change(sample, monkeypatch):
    _, destination, service = sample
    preview = run(sample)
    original = export._snapshot
    calls = []
    def racing(*args, **kwargs):
        calls.append(True)
        if len(calls) == 2:
            service.notes["Notes/C.md"]["text"] += "Concurrent edit."
        return original(*args, **kwargs)
    monkeypatch.setattr(export, "_snapshot", racing)
    with pytest.raises(ValueError, match="changed before export"):
        run(sample, apply=True, preview_identity=preview["preview_identity"])
    assert not destination.exists()
    assert not list(destination.parent.glob(".related-export-*"))


def test_preview_identity_binds_rendered_files_not_only_source_and_settings(sample, monkeypatch):
    _, destination, _ = sample
    preview = run(sample)
    original = export.project_related_links
    monkeypatch.setattr(export, "project_related_links", lambda *args, **kwargs: original(*args, **kwargs) + "\nChanged export template.\n")
    changed = run(sample)
    assert changed["preview_identity"] != preview["preview_identity"]
    with pytest.raises(ValueError, match="exact current preview"):
        run(sample, apply=True, preview_identity=preview["preview_identity"])
    assert not destination.exists()


@pytest.mark.parametrize("change", ["truncated", "unavailable", "dependency", "unresolved", "unbound", "unaccepted"])
def test_unavailable_or_unbound_graph_cannot_be_written(sample, monkeypatch, change):
    _, destination, service = sample
    if change == "truncated":
        index = service.index
        monkeypatch.setattr(service, "index", lambda workspace_id: {**index(workspace_id), "truncated": True})
    elif change == "unavailable":
        service.semantic["status"] = "unavailable"
    elif change in {"dependency", "unresolved"}:
        service.semantic["status"] = "partial"
        service.semantic["coverage"].update(stale=1)
        service.semantic["coverage"]["dependency_budget_reached" if change == "dependency" else "unresolved"] = True if change == "dependency" else 1
    elif change == "unbound":
        service.semantic["note_revisions"]["Notes/A.md"] = "old"
    else:
        service.edges[0]["accepted"] = True
    with pytest.raises(ValueError):
        run(sample)
    assert not destination.exists()


@pytest.mark.parametrize("path", ["../escape.md", "/absolute.md", "a//b.md", "Folder\\name.md"])
def test_unsafe_automatic_paths_are_not_silently_rewritten(sample, path):
    root, destination, _ = sample
    service = SnapshotDocuments(root, {"a.md": "A", path: "B"}, [edge("a.md", path)])
    with pytest.raises(ValueError):
        export.export_markdown_links(root, destination, documents=service, workspace_id="workspace")
    assert not destination.exists()


@pytest.mark.parametrize("target", ["Elsewhere/a#b.md", "Elsewhere/a|b.md", "Elsewhere/a[b].md", "Elsewhere/a%b.md", "Elsewhere/café[1].md"])
def test_delimited_filenames_use_encoded_relative_markdown_without_renaming(sample, target):
    from urllib.parse import unquote
    import posixpath
    import re
    root, destination, _ = sample
    service = SnapshotDocuments(root, {"Folder/A.md": "Authored [[Manual]].\n", target: "Other finding."}, [edge("Folder/A.md", target)])
    preview = export.export_markdown_links(root, destination, documents=service, workspace_id="workspace")
    export.export_markdown_links(root, destination, documents=service, workspace_id="workspace", apply=True, preview_identity=preview["preview_identity"])
    text = (destination / "Folder/A.md").read_text()
    href = re.search(r"\]\(([^)]+)\)", text)[1]
    assert posixpath.normpath(posixpath.join("Folder", unquote(href))) == target
    assert "#" not in href and "[" not in href and "|" not in href
    assert (destination / target).is_file()
    assert text.startswith("Authored [[Manual]].\n")


@pytest.mark.parametrize("pair", [("A.md", "a.md"), ("café.md", "cafe\u0301.md")])
def test_portable_case_and_unicode_collisions_are_rejected(sample, pair):
    root, destination, _ = sample
    service = SnapshotDocuments(root, {path: path for path in pair}, [edge(*pair)])
    with pytest.raises(ValueError, match="collide"):
        export.export_markdown_links(root, destination, documents=service, workspace_id="workspace")


def test_projection_removes_only_exact_legacy_empty_block_and_preserves_manual_and_typed_links():
    empty = f"## Graph Links\n\n{GRAPH_START_MARKER}\nNo committed typed links or canonical clusters yet.\n{GRAPH_END_MARKER}\n"
    manual = "Manual [[My note]] stays.  \n\n## Related notes — automatic\nA human-owned heading.\n\n"
    projected = export.project_related_links(manual + empty, ["Folder/Other.md"])
    assert projected.startswith(manual)
    assert "No committed typed links" not in projected
    assert "[[Folder/Other|Other]]" in projected
    assert export.project_related_links(projected, ["Folder/Other.md"]) == projected
    typed = f"## Graph Links\n\n{GRAPH_START_MARKER}\n- supports: [[Existing claim]]\n{GRAPH_END_MARKER}\n"
    assert export.project_related_links(manual + typed, []).startswith(manual + typed)


@pytest.mark.parametrize("text", [RELATED_START_MARKER, RELATED_END_MARKER,
    f"{RELATED_START_MARKER}\nx\n{RELATED_START_MARKER}\nx\n{RELATED_END_MARKER}\n",
    f"{RELATED_END_MARKER}\nx\n{RELATED_START_MARKER}\n",
    "<!-- auto-zettelkasten:related:finish -->\n",
    f"{RELATED_START_MARKER}\n{GRAPH_START_MARKER}\nx\n{GRAPH_END_MARKER}\n{RELATED_END_MARKER}\n",
    f"{GRAPH_START_MARKER}\n{RELATED_START_MARKER}\nx\n{RELATED_END_MARKER}\n{GRAPH_END_MARKER}\n"])
def test_ambiguous_or_nested_owned_blocks_are_rejected(text):
    with pytest.raises(ValueError):
        export.project_related_links(text, [])
    with pytest.raises(ValueError):
        export.strip_related_links(text)


def test_fenced_marker_examples_are_preserved_and_generated_links_are_not_embedded():
    original = f"# A\n\n```md\n{RELATED_START_MARKER}\nAn example [[Example]].\n{RELATED_END_MARKER}\n```\n\nSubstantive claim.\n"
    projected = export.project_related_links(original, ["Notes/B.md"])
    assert projected.startswith(original)
    assert export.strip_related_links(projected).rstrip() == original.rstrip()
    assert semantic_note_hash(projected) == semantic_note_hash(original)


def test_crlf_and_bom_prefix_are_preserved(sample):
    _, destination, service = sample
    row = service.notes["Notes/A.md"]
    row.update(text="# A\r\n\r\nKeep manual [[B]] and trailing spaces.  \r\n", bom=True)
    preview = run(sample)
    run(sample, apply=True, preview_identity=preview["preview_identity"])
    raw = (destination / "Notes/A.md").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf" + row["text"].encode())
    assert b"\n" not in raw.replace(b"\r\n", b"")


def test_partial_stale_note_exports_effective_text_without_stale_incident_links(sample):
    _, destination, service = sample
    service.notes["Notes/A.md"].update(text="Personal override with [[B]].\n", override_saved=True)
    service.semantic.update(status="partial")
    service.semantic["coverage"]["stale"] = 1
    service.semantic["note_revisions"].pop("Notes/A.md")
    service.edges = [edge("Notes/B.md", "Notes/C.md")]
    preview = run(sample)
    assert preview["stale_source_count"] == 1 and preview["override_count"] == 1 and preview["edge_count"] == 1
    run(sample, apply=True, preview_identity=preview["preview_identity"])
    a = (destination / "Notes/A.md").read_text()
    assert a.startswith("Personal override with [[B]].\n")
    assert "No automatic connections selected" in a
    assert "[[Notes/A|A]]" not in (destination / "Notes/B.md").read_text()


@pytest.mark.parametrize("personal_metadata", [False, True])
def test_real_document_service_preserves_effective_personal_override_and_original_files(tmp_path, personal_metadata):
    root = tmp_path / "source"
    notes, profiles = root / "02_source_memory/notes", root / "02_source_memory/profiles"
    notes.mkdir(parents=True)
    profiles.mkdir()
    (root / "auto-zettelkasten.yml").write_text("engine_version: synthetic\n")
    sources = []
    for name in ("a", "b"):
        file = notes / f"{name}.md"
        file.write_text(f"---\nsource_id: source-{name}\nnote_id: note-{name}\n---\n# {name}\nA substantive claim.\n")
        profile = profiles / f"note-{name}.yml"
        profile.write_text(f"profile:\n  source_id: source-{name}\n  note_id: note-{name}\n")
        sources.append({"source_id": f"source-{name}", "note_id": f"note-{name}", "note_path": file.relative_to(root).as_posix(),
                        "projection_hash": hashlib.sha256(file.read_bytes()).hexdigest(), "profile_file_sha256": hashlib.sha256(profile.read_bytes()).hexdigest(),
                        "chunk_ids": [], "coverage": {"status": "included"}})
    (root / "Personal.md").write_text("# Personal\nMy original thought.\n")
    mapping = root / ".auto-zettelkasten/mapping/result.json"
    mapping.parent.mkdir(parents=True)
    mapping.write_text(json.dumps({"schema_version": "local-semantic-map-v1", "identity": {"digest": "a" * 64},
                                  "sources": sources, "communities": [],
                                  "neighbors": [{"source_id": "source-a", "target_source_id": "source-b", "score": .8, "provenance": {}}]}))
    overrides = tmp_path / "overrides"
    service = DocumentService(journal_root=tmp_path / "operations", override_root=overrides)
    try:
        workspace_id = service.open(root)["id"]
        doc = service.read(workspace_id, "Personal.md")
        service.save_override(workspace_id, doc["id"], doc["revision"], "# Personal\nMy edited thought and [[02_source_memory/notes/a]].\n")
        if personal_metadata:
            doc = service.read(workspace_id, "02_source_memory/notes/a.md")
            service.save_personal_metadata(workspace_id, doc["id"], doc["revision"],
                                           {"display_title": "Personal source title", "personal_properties": {"tags": ["reviewed"]}})
        before = {str(p): p.read_bytes() for base in (root, overrides) for p in base.rglob("*") if p.is_file()}
        destination = tmp_path / "portable"
        preview = export.export_markdown_links(root, destination, documents=service, workspace_id=workspace_id)
        assert preview["override_count"] == 1 + personal_metadata and preview["edge_count"] == 1
        export.export_markdown_links(root, destination, documents=service, workspace_id=workspace_id, apply=True, preview_identity=preview["preview_identity"])
        assert "My edited thought" in (destination / "Personal.md").read_text()
        assert "My original thought" in (root / "Personal.md").read_text()
        if personal_metadata:
            assert preview["personal_title_count"] == preview["personal_property_count"] == 1
            assert "[[02_source_memory/notes/a|Personal source title]]" in (destination / "02_source_memory/notes/b.md").read_text()
            assert "Personal source title" not in (root / "02_source_memory/notes/a.md").read_text()
        assert before == {str(p): p.read_bytes() for base in (root, overrides) for p in base.rglob("*") if p.is_file()}
    finally:
        service.close()


def test_fresh_export_projects_personal_metadata_and_labels_without_renaming(sample):
    root, destination, service = sample
    original = "---\n# Keep this comment\ntitle: Original source title # source title comment\nsource_id: stable-source\nnote_id: stable-note\ntags:\n- generated\naliases: [Original alias]\nrating: 2\n---\n# Authored heading\nManual [[B|My label]] stays.  \n"
    row = service.notes["Notes/A.md"]
    row.update(text=original, display_title="My research title", personal_display_title="My research title",
               personal_properties={"tags": ["generated", "#personal"], "aliases": ["Personal alias"], "rating": 5,
                                    "reviewed": True, "due": "2026-10-28"})
    before = {str(path): path.read_bytes() for path in root.rglob("*.md")}
    preview = run(sample)
    assert preview["personal_metadata_count"] == preview["personal_title_count"] == 1
    assert preview["personal_property_count"] == 5 and preview["personal_property_conflict_count"] == 1
    run(sample, apply=True, preview_identity=preview["preview_identity"])
    exported = (destination / "Notes/A.md").read_text()
    fields = yaml.safe_load(exported.split("---\n", 2)[1])
    assert fields == {"title": "My research title", "source_id": "stable-source", "note_id": "stable-note",
                      "tags": ["generated", "personal"], "aliases": ["Original alias", "Personal alias", "My research title"],
                      "rating": 2, "reviewed": True, "due": "2026-10-28"}
    assert "# Keep this comment" in exported and "# source title comment" in exported
    assert "# Authored heading\nManual [[B|My label]] stays.  \n" in exported
    assert "[[Notes/A|My research title]]" in (destination / "Notes/B.md").read_text()
    assert {str(path): path.read_bytes() for path in root.rglob("*.md")} == before
    manifest = json.loads((destination / ".auto-zettelkasten/related-export.json").read_text())
    provenance = manifest["personal_metadata"]["Notes/A.md"]
    assert provenance["canonical_title"] == "Original source title"
    assert provenance["source_identity"] == {"source_id": "stable-source", "note_id": "stable-note"}
    assert provenance["property_conflicts"] == ["rating"]
    assert run(sample, apply=True, preview_identity=preview["preview_identity"])["status"] == "unchanged"
    assert sorted(path.relative_to(destination).as_posix() for path in destination.rglob("*.md")) == sorted(service.notes)


def test_personal_metadata_change_invalidates_preview_and_preserves_frozen_export(sample):
    _, destination, service = sample
    preview = run(sample)
    service.notes["Notes/A.md"].update(display_title="New title", personal_display_title="New title")
    with pytest.raises(ValueError, match="exact current preview"):
        run(sample, apply=True, preview_identity=preview["preview_identity"])
    preview = run(sample)
    run(sample, apply=True, preview_identity=preview["preview_identity"])
    frozen = {str(path): path.read_bytes() for path in destination.rglob("*") if path.is_file()}
    service.notes["Notes/A.md"].update(display_title="Another title", personal_display_title="Another title")
    with pytest.raises(ValueError, match="edited or differs"):
        run(sample)
    assert {str(path): path.read_bytes() for path in destination.rglob("*") if path.is_file()} == frozen


def test_export_metadata_preserves_bom_crlf_and_handles_absent_frontmatter():
    text = "\ufeff# Manual heading\r\nAuthored [[Other]].  \r\n"
    projected, _ = export._project_personal_metadata(text, "A personal title", {"tags": ["one"], "score": .5})
    assert projected.startswith("\ufeff---\r\n")
    assert projected.endswith(text[1:])
    assert "\n" not in projected.replace("\r\n", "")
    assert yaml.safe_load(projected[1:].split("---\r\n", 2)[1])["title"] == "A personal title"


@pytest.mark.parametrize("header", ["---\ntags: [broken\n---\n", "---\ntitle: First\ntitle: Second\n---\n",
                                       "---\ntags: &t [one]\naliases: *t\n---\n", "---\nnever closes\n"])
def test_unsafe_header_cannot_be_rewritten_for_personal_export_metadata(header):
    with pytest.raises(ValueError):
        export._project_personal_metadata(header + "Manual body", "Personal", {})
    assert export._project_personal_metadata(header + "Manual body", None, {}) == (header + "Manual body", None)


def test_display_title_delimiters_use_safe_links_and_duplicate_titles_keep_paths(sample):
    _, destination, service = sample
    for path in ("Notes/A.md", "Notes/C.md"):
        service.notes[path].update(display_title="Same [title] | label", personal_display_title="Same [title] | label")
    preview = run(sample)
    run(sample, apply=True, preview_identity=preview["preview_identity"])
    text = (destination / "Notes/B.md").read_text()
    assert "[Same \\[title\\] | label](A.md)" in text
    assert (destination / "Notes/A.md").is_file() and (destination / "Notes/C.md").is_file()


@pytest.mark.parametrize("header", ["---\n---\n", "---\n# Empty properties\n---\n",
                                       "---\ntitle: |\n  Multiline\n  source title\nsource_id: stable\n---\n"])
def test_personal_title_projection_handles_empty_and_multiline_headers(header):
    text, _ = export._project_personal_metadata(header + "Authored body.\n", "Personal title", {})
    fields = yaml.safe_load(text.split("---\n", 2)[1])
    assert fields["title"] == "Personal title" and fields["aliases"] == ["Personal title"]
    if "source_id" in header:
        assert fields["source_id"] == "stable"
    assert text.endswith("Authored body.\n")
