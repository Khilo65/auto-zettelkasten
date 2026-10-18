from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path

import pytest
import yaml

from auto_zettelkasten import documents
from auto_zettelkasten.documents import DocumentConflict, DocumentError, DocumentIndexChanged, DocumentService


@pytest.fixture
def catalogue(tmp_path):
    root = tmp_path.resolve()
    service = DocumentService(journal_root=root.parent / (root.name + "-recovery"))
    yield root, service
    service.close()


def write(root, path, text):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def collect(service, workspace_id, **options):
    pages = [service.index_page(workspace_id, **options)]
    while not pages[-1]["complete"]:
        assert pages[-1]["next_cursor"]
        pages.append(service.index_page(workspace_id, pages[-1]["next_cursor"]))
        assert len(pages) < 20000
    return pages, {key: [row for page in pages for row in page[key]] for key in ("documents", "entries", "relations")}


def generated(root, rows, *, padding=0):
    write(root, "auto-zettelkasten.yml", "engine_version: test\n")
    write(root, "02_source_memory/notes/One.md", "---\nnote_id: note-one\n---\n# One\n")
    write(root, "02_source_memory/notes/Two.md", "---\nnote_id: note-two\n---\n# Two\n")
    registry = {"registry_schema_version": documents.RELATIONSHIP_REGISTRY_SCHEMA_VERSION,
                "links": rows, "private_payload": "private rationale " + "x" * padding}
    return write(root, "02_source_memory/indexes/typed_links.yml", json.dumps(registry))


def relation(identity="edge-1", **updates):
    return {"relation_id": identity, "relation_type": "supports", "source_note_id": "note-one",
            "target_note_id": "note-two", "active": True, "decision_status": "accepted", **updates}


def test_pages_cover_full_catalogue_past_legacy_entry_and_byte_limits(catalogue):
    root, service = catalogue
    text = "# Note\n" + "a" * 2300
    for at in range(7566):
        write(root, f"Note {at:05}.md", text)
    workspace = service.open(root)
    assert workspace["truncated"]
    pages, result = collect(service, workspace["id"])
    assert len(result["documents"]) == len(result["entries"]) == 7566
    assert len({row["path"] for row in result["documents"]}) == 7566
    assert all(len(page["documents"]) <= 32 for page in pages)
    assert all(sum(len(row["text"].encode()) for row in page["documents"]) <= documents.INDEX_PAGE_BYTES for page in pages)
    assert pages[-1]["progress"]["documents"] == 7566
    assert pages[-1]["progress"]["bytes"] > documents.MAX_TREE_BYTES
    assert all(not page["truncated"] and page["warnings"] == [] for page in pages)
    assert not service._documents and not service._document_ids


def test_byte_budget_and_bom_newlines_are_preserved(catalogue):
    root, service = catalogue
    for name in ("One.md", "Two.md", "Three.md"):
        (root / name).write_bytes(b"\xef\xbb\xbf# Note\r\n" + b"x" * 600000)
    workspace = service.open(root)
    pages, result = collect(service, workspace["id"])
    assert len([page for page in pages if page["documents"]]) == 3
    assert all(not row["text"].startswith("\ufeff") and row["text"].startswith("# Note\r\n") for row in result["documents"])
    assert all(row["revision"] == hashlib.sha256((root / row["path"]).read_bytes()).hexdigest() for row in result["documents"])


def test_cursor_retry_is_idempotent_and_cross_root_cursors_are_rejected(catalogue, monkeypatch):
    root, service = catalogue
    monkeypatch.setattr(documents, "INDEX_PAGE_DOCUMENTS", 1)
    for name in ("One.md", "Two.md", "Three.md"):
        write(root, name, "note")
    workspace = service.open(root)
    first = service.index_page(workspace["id"])
    second = service.index_page(workspace["id"], first["next_cursor"])
    expected = copy.deepcopy(second)
    second["documents"].clear()
    assert service.index_page(workspace["id"], first["next_cursor"]) == expected
    third = service.index_page(workspace["id"], expected["next_cursor"])
    with pytest.raises(DocumentError):
        service.index_page(workspace["id"], first["next_cursor"])
    other = root.parent / (root.name + "-other")
    other.mkdir()
    write(other, "One.md", "other root")
    opened = service.open(other)
    with pytest.raises(DocumentError):
        service.index_page(opened["id"], third["next_cursor"])
    with pytest.raises(DocumentError):
        service.index_page(workspace["id"], third["next_cursor"])


def test_completed_generation_refresh_checks_metadata_without_reading_bodies(catalogue, monkeypatch):
    root, service = catalogue
    for at in range(5):
        write(root, f"Note {at}.md", "note")
    workspace = service.open(root)
    pages, _ = collect(service, workspace["id"])
    monkeypatch.setattr(documents, "INDEX_PAGE_OPERATIONS", 2)
    monkeypatch.setattr(service, "_read_bytes", lambda *args: pytest.fail("Unchanged generation reread a note body"))
    refreshed, result = collect(service, workspace["id"], known_generation=pages[-1]["generation"])
    assert len(refreshed) > 1 and refreshed[-1]["not_modified"]
    assert refreshed[-1]["generation"] == pages[-1]["generation"]
    assert result == {"documents": [], "entries": [], "relations": []}


@pytest.mark.parametrize("change", ["edit", "add", "remove", "rename", "chmod", "marker"])
def test_mutation_after_an_earlier_page_invalidates_the_whole_generation(catalogue, monkeypatch, change):
    root, service = catalogue
    monkeypatch.setattr(documents, "INDEX_PAGE_DOCUMENTS", 1)
    for name in ("One.md", "Two.md", "Three.md"):
        write(root, name, "original")
    workspace = service.open(root)
    first = service.index_page(workspace["id"])
    path = root / first["documents"][0]["path"]
    if change == "edit":
        path.write_text("different")
    elif change == "add":
        write(root, "Added.md", "new")
    elif change == "remove":
        path.unlink()
    elif change == "rename":
        path.rename(root / "Renamed.md")
    elif change == "chmod":
        path.chmod(0o400)
    else:
        write(root, "auto-zettelkasten.yml", "engine_version: test\n")
    with pytest.raises(DocumentIndexChanged):
        cursor = first["next_cursor"]
        while cursor:
            page = service.index_page(workspace["id"], cursor)
            assert not page["complete"]
            cursor = page["next_cursor"]


def test_failed_refresh_preserves_previous_completed_generation(catalogue):
    root, service = catalogue
    note = write(root, "One.md", "original")
    workspace = service.open(root)
    pages, _ = collect(service, workspace["id"])
    completed = service._completed_index
    note.write_text("external edit")
    with pytest.raises(DocumentIndexChanged):
        collect(service, workspace["id"], known_generation=pages[-1]["generation"])
    assert service._completed_index is completed
    _, result = collect(service, workspace["id"])
    assert result["documents"][0]["text"] == "external edit"


def test_root_replacement_invalidates_continuations(catalogue, monkeypatch):
    root, service = catalogue
    monkeypatch.setattr(documents, "INDEX_PAGE_DOCUMENTS", 1)
    write(root, "One.md", "original")
    write(root, "Two.md", "original")
    workspace = service.open(root)
    first = service.index_page(workspace["id"])
    old = root.with_name(root.name + "-old")
    root.rename(old)
    root.mkdir()
    write(root, "Outside.md", "replacement content")
    with pytest.raises(DocumentIndexChanged):
        service.index_page(workspace["id"], first["next_cursor"])


@pytest.mark.parametrize("padding", [4 * 1024 * 1024, 20 * 1024 * 1024])
def test_large_registry_projects_only_public_relations_after_all_note_pages(catalogue, monkeypatch, padding):
    root, service = catalogue
    monkeypatch.setattr(documents, "INDEX_PAGE_DOCUMENTS", 1)
    registry = generated(root, [relation(private_rationale="not public"), relation("inactive", active=False)], padding=padding)
    original = registry.read_bytes()
    workspace = service.open(root)
    pages, result = collect(service, workspace["id"])
    assert result["relations"] == [{"id": "edge-1", "source": "02_source_memory/notes/One.md", "target": "02_source_memory/notes/Two.md", "type": "supports"}]
    assert pages[-1]["progress"]["relations"] == 1
    assert all(not row["editable"] for row in result["documents"])
    assert all(not page["warnings"] and not page["truncated"] for page in pages)
    assert "private_payload" not in json.dumps(pages) and "not public" not in json.dumps(pages)
    assert registry.read_bytes() == original


def test_registry_uses_its_own_bound_and_never_relaxes_markdown_limit(catalogue, monkeypatch):
    root, service = catalogue
    generated(root, [relation()], padding=2000)
    write(root, "Large.md", "x" * (documents.MAX_FILE_BYTES + 1))
    workspace = service.open(root)
    monkeypatch.setattr(documents, "MAX_REGISTRY_BYTES", 1024)
    pages, result = collect(service, workspace["id"])
    assert len(result["documents"]) == 2 and result["relations"] == []
    assert any(row["path"] == "Large.md" and not row["editable"] for row in result["entries"])
    assert pages[-1]["truncated"] and pages[-1]["warnings"]
    assert documents.MAX_FILE_BYTES == 1024 * 1024


@pytest.mark.parametrize("content", [
    "registry_schema_version: '7'\nlinks: &rows []\nprivate: *rows\n",
    "registry_schema_version: '7'\nlinks: []\n---\nlinks: []\n",
    "registry_schema_version: '6'\nlinks: []\n",
    "registry_schema_version: '7'\nlinks: {}\n",
    "registry_schema_version: '7'\nlinks: [",
])
def test_unsupported_registry_payload_does_not_publish_partial_relations(catalogue, content):
    root, service = catalogue
    registry = generated(root, [])
    registry.write_text(content)
    workspace = service.open(root)
    pages, result = collect(service, workspace["id"])
    assert len(result["documents"]) == 2 and result["relations"] == []
    assert pages[-1]["warnings"] and pages[-1]["truncated"]
    assert str(registry) not in json.dumps(pages)


def test_registry_changes_mid_generation_are_rejected(catalogue, monkeypatch):
    root, service = catalogue
    monkeypatch.setattr(documents, "INDEX_PAGE_DOCUMENTS", 1)
    registry = generated(root, [relation()])
    workspace = service.open(root)
    first = service.index_page(workspace["id"])
    registry.write_text(registry.read_text() + " ")
    with pytest.raises(DocumentIndexChanged):
        service.index_page(workspace["id"], first["next_cursor"])


def test_canonical_source_and_map_cluster_paths_win_over_compatibility_copies(catalogue):
    root, service = catalogue
    generated(root, [relation(), relation("has", relation_type="has_member", source_kind="cluster", source_id="cluster-one", source_note_id=None),
                     relation("member", relation_type="cluster_member", target_cluster_id="cluster-one", target_note_id=None)])
    write(root, "evaluation/delta_payload/Copy.md", "---\nnote_id: note-one\n---\ncopy")
    write(root, "03_literature_synthesis/clusters/Legacy.md", "---\ncluster_id: cluster-one\n---\nlegacy")
    cluster = "03_literature_synthesis/maps/map-one/clusters/Canonical.md"
    write(root, cluster, "---\ncluster_id: cluster-one\n---\ncanonical")
    workspace = service.open(root)
    pages, result = collect(service, workspace["id"])
    assert len(result["relations"]) == 3
    by_id = {row["id"]: row for row in result["relations"]}
    assert by_id["edge-1"]["source"] == "02_source_memory/notes/One.md"
    assert by_id["has"]["source"] == cluster and by_id["member"]["target"] == cluster
    assert pages[-1]["warnings"] == []
    write(root, "02_source_memory/notes/Conflicting.md", "---\nnote_id: note-one\n---\nconflict")
    pages, result = collect(service, workspace["id"])
    assert not any(row["id"] in {"edge-1", "member"} for row in result["relations"])
    assert pages[-1]["warnings"] and pages[-1]["truncated"]


@pytest.mark.parametrize("linked", ["symlink", "hardlink"])
def test_special_registry_links_are_not_followed(catalogue, linked):
    root, service = catalogue
    registry = generated(root, [relation()])
    outside = root.parent / (root.name + "-outside.yml")
    registry.rename(outside)
    if linked == "symlink":
        registry.symlink_to(outside)
    else:
        os.link(outside, registry)
    workspace = service.open(root)
    pages, result = collect(service, workspace["id"])
    assert result["relations"] == [] and pages[-1]["warnings"]
    assert "private rationale" not in json.dumps(pages)


def test_paged_index_does_not_refresh_open_save_handle_identities(catalogue):
    root, service = catalogue
    note = write(root, "One.md", "original")
    workspace = service.open(root)
    opened = service.read(workspace["id"], "One.md")
    handles = dict(service._documents)
    replacement = write(root, "replacement.tmp", "original")
    replacement.replace(note)
    collect(service, workspace["id"])
    assert service._documents == handles
    if documents._SAFE_WRITES:
        with pytest.raises(DocumentConflict):
            service.save(workspace["id"], opened["id"], opened["revision"], "edited")


def test_existing_publication_marker_content_change_invalidates_known_generation(catalogue):
    root, service = catalogue
    generated(root, [relation()])
    workspace = service.open(root)
    pages, _ = collect(service, workspace["id"])
    (root / "auto-zettelkasten.yml").write_text("engine_version: changed\n")
    with pytest.raises(DocumentIndexChanged):
        collect(service, workspace["id"], known_generation=pages[-1]["generation"])


def test_resource_bound_is_an_explicit_failure_instead_of_a_complete_prefix(catalogue, monkeypatch):
    root, service = catalogue
    write(root, "One.md", "one")
    write(root, "Two.md", "two")
    workspace = service.open(root)
    monkeypatch.setattr(documents, "MAX_INDEX_PATHS", 2)
    with pytest.raises(DocumentError, match="resource limit"):
        collect(service, workspace["id"])
    assert service._completed_index is None


def test_note_symlink_race_does_not_read_outside_the_selected_root(catalogue, monkeypatch):
    root, service = catalogue
    if not documents._DIR_HANDLES:
        pytest.skip("Descriptor-relative reads are unavailable")
    note = write(root, "Note.md", "original")
    outside = write(root.parent, root.name + "-outside.md", "outside secret")
    workspace = service.open(root)
    original = os.open

    def swap(path, flags, *args, **kwargs):
        if path == "Note.md" and not flags & os.O_CREAT:
            note.unlink()
            note.symlink_to(outside)
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap)
    with pytest.raises(DocumentIndexChanged):
        collect(service, workspace["id"])


def test_engine_row_aliases_project_public_fields_and_skip_shared_private_metadata(catalogue):
    root, service = catalogue
    registry = generated(root, [])
    evidence = [{"private_rationale": "never public", "anchors": ["private-one", "private-two"]}]
    row = relation(evidence=evidence)
    payload = {"registry_schema_version": "7", "relations": [row], "links": [row],
               "pair_decisions": [{"source_evidence": evidence}], "events": [{"payload": evidence[0]}]}
    serialized = yaml.safe_dump(payload, sort_keys=False)
    assert "&id" in serialized and "*id" in serialized
    registry.write_text(serialized)
    workspace = service.open(root)
    pages, result = collect(service, workspace["id"])
    assert result["relations"] == [{"id": "edge-1", "source": "02_source_memory/notes/One.md", "target": "02_source_memory/notes/Two.md", "type": "supports"}]
    assert pages[-1]["warnings"] == [] and not pages[-1]["truncated"]
    assert "never public" not in json.dumps(pages) and "private-one" not in json.dumps(pages)


def test_private_alias_graphs_are_skipped_without_expanding_them(catalogue):
    root, service = catalogue
    registry = generated(root, [])
    row = relation()
    serialized = yaml.safe_dump({"registry_schema_version": "7", "relations": [row], "links": [row]}, sort_keys=False)
    serialized += "private_graph:\n  a: &a [private, private]\n"
    previous = "a"
    for at in range(30):
        name = f"level{at}"
        serialized += f"  {name}: &{name} [*{previous}, *{previous}]\n"
        previous = name
    registry.write_text(serialized)
    workspace = service.open(root)
    pages, result = collect(service, workspace["id"])
    assert len(result["relations"]) == 1 and not pages[-1]["truncated"]


@pytest.mark.parametrize("content", [
    "registry_schema_version: '7'\nlinks: [*undefined]\n",
    "registry_schema_version: '7'\nprivate: &identifier note-one\nlinks:\n- relation_id: edge\n  relation_type: supports\n  source_note_id: *identifier\n  target_note_id: note-two\n  decision_status: accepted\n",
    "registry_schema_version: '7'\nrelations: &rows []\nlinks: *rows\n",
    "registry_schema_version: null\nregistry_schema_version: '7'\nlinks: []\n",
    "registry_schema_version: '7'\nlinks: []\nprivate: !!python/object:builtins.object {}\n",
])
def test_aliases_cannot_supply_relation_fields_or_arbitrary_link_collections(catalogue, content):
    root, service = catalogue
    registry = generated(root, [])
    registry.write_text(content)
    workspace = service.open(root)
    pages, result = collect(service, workspace["id"])
    assert result["relations"] == [] and pages[-1]["truncated"]
    assert pages[-1]["warnings"]
