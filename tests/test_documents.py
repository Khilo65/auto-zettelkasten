from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import socket
import stat
import subprocess

import pytest

from auto_zettelkasten import documents
from auto_zettelkasten.documents import DocumentConflict, DocumentError, DocumentService


@pytest.fixture
def service(tmp_path):
    value = DocumentService(journal_root=tmp_path.parent / (tmp_path.name + "-recovery"))
    yield value
    value.close()


@pytest.fixture
def writable():
    if not documents._SAFE_WRITES:
        pytest.skip("Safe descriptor-relative writes are unavailable on this platform")


def test_offline_open_tree_read_create_and_save(tmp_path, service, monkeypatch, writable):
    def unexpected(*args, **kwargs):
        pytest.fail("Document operations must not use the network or launch a process")

    monkeypatch.setattr(socket, "create_connection", unexpected)
    monkeypatch.setattr(subprocess, "Popen", unexpected)
    note = tmp_path / "Thought.md"
    original = b"---\ntitle: My thought\naliases: [Thought]\n---\n\n# First\n"
    note.write_bytes(original)
    workspace = service.open(tmp_path)
    assert workspace["name"] == tmp_path.name
    assert workspace["path"] == str(tmp_path)
    assert workspace["editable"] is True
    assert workspace["entries"] == [{"path": "Thought.md", "name": "Thought.md", "kind": "file", "editable": True, "reason": ""}]
    doc = service.read(workspace["id"], "Thought.md")
    assert doc["text"].encode() == original
    assert doc["revision"] == hashlib.sha256(original).hexdigest()
    assert doc["id"] != doc["path"]
    assert doc["id"] == service.read(workspace["id"], "Thought.md")["id"]
    saved = service.save(workspace["id"], doc["id"], doc["revision"], doc["text"] + "New text.\n")
    assert saved["id"] == doc["id"]
    assert saved["recovery_key"] == doc["recovery_key"]
    assert note.read_bytes() == original + b"New text.\n"
    result = service.create(workspace["id"], "", "Drafts", "directory")
    assert "document" not in result
    created = service.create(workspace["id"], "Drafts", "New note", "file")
    assert created["document"]["path"] == "Drafts/New note.md"
    assert created["document"]["text"] == ""
    assert (tmp_path / "Drafts" / "New note.md").read_bytes() == b""
    assert not list(tmp_path.rglob(".az-*.tmp"))


@pytest.mark.parametrize("bom,newline", [(False, "\n"), (True, "\n"), (False, "\r\n"), (True, "\r\n")])
def test_exact_bom_and_line_endings_survive_normalized_editor_text(tmp_path, service, writable, bom, newline):
    text = newline.join(["---", "title: café", "---", "", "# Note", ""])
    original = (b"\xef\xbb\xbf" if bom else b"") + text.encode()
    note = tmp_path / "Note.md"
    note.write_bytes(original)
    workspace = service.open(tmp_path)
    doc = service.read(workspace["id"], "Note.md")
    assert doc["bom"] is bom
    assert doc["newline"] == ("crlf" if newline == "\r\n" else "lf")
    assert doc["text"] == text
    initial_inode = note.stat().st_ino
    same = service.save(workspace["id"], doc["id"], doc["revision"], text.replace("\r\n", "\n"))
    assert note.read_bytes() == original
    assert note.stat().st_ino == initial_inode
    changed = service.save(workspace["id"], same["id"], same["revision"], text.replace("\r\n", "\n") + "Added.\n")
    assert note.read_bytes() == original + f"Added.{newline}".encode()
    assert changed["revision"] == hashlib.sha256(note.read_bytes()).hexdigest()


def test_opening_another_root_and_close_invalidate_ids_but_recovery_key_is_stable(tmp_path, service):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "Note.md").write_text("one")
    opened = service.open(first)
    doc = service.read(opened["id"], "Note.md")
    service.open(second)
    with pytest.raises(DocumentError):
        service.tree(opened["id"])
    reopened = service.open(first)
    new_doc = service.read(reopened["id"], "Note.md")
    assert reopened["id"] != opened["id"]
    assert new_doc["id"] != doc["id"]
    assert new_doc["recovery_key"] == doc["recovery_key"]
    service.close()
    with pytest.raises(DocumentError):
        service.read(reopened["id"], "Note.md")


@pytest.mark.parametrize("path", ["../outside.md", "/outside.md", "folder/../Note.md", "folder//Note.md", "./Note.md", "folder\\Note.md", "C:/Note.md", ".git/note.md", "credentials/password.md", "11_state/note.md", "profiles/note.md", "node_modules/note.md", "secrets.md", "Note.md\x00", "Note.md ", "NUL.md"])
def test_read_and_create_reject_the_same_disallowed_paths(tmp_path, service, path, writable):
    workspace = service.open(tmp_path)
    with pytest.raises(DocumentError):
        service.read(workspace["id"], path)
    with pytest.raises(DocumentError):
        service.create(workspace["id"], "", path, "file")


def test_tree_excludes_hidden_private_nonmarkdown_and_symlink_targets(tmp_path, service):
    for folder in (".git", ".codex", ".venv", "credentials", "node_modules", "01_custody", "11_state", "profiles", "indexes", "private"):
        directory = tmp_path / folder
        directory.mkdir()
        (directory / "hidden.md").write_text("not a note")
    (tmp_path / "secret.md").write_text("secret")
    (tmp_path / "keep.md").write_text("keep")
    (tmp_path / "not-markdown.txt").write_text("ignore")
    workspace = service.open(tmp_path)
    assert [row["path"] for row in workspace["entries"]] == ["keep.md"]
    for folder in (".codex", "credentials", "11_state", "profiles", "private"):
        with pytest.raises(DocumentError):
            service.open(tmp_path / folder)
    if documents._DIR_HANDLES:
        (tmp_path / "link.md").symlink_to(tmp_path / "keep.md")
        (tmp_path / "linked").symlink_to(tmp_path / ".codex", target_is_directory=True)
        assert [row["path"] for row in service.tree(workspace["id"])["entries"]] == ["keep.md"]
        with pytest.raises(DocumentError):
            service.read(workspace["id"], "link.md")
        with pytest.raises(DocumentError):
            service.open(tmp_path / "linked")


@pytest.mark.parametrize("text", ["<!-- auto-zettelkasten:graph:start -->\ntext", "---\nsource_id: source-1\n---\nbody", "---\nnote_id: note-1\n---\nbody", "---\nmachine_preservation_hash: abc\n---\nbody", "---\ncluster_id: cluster-1\n---\nbody"])
def test_copied_generated_notes_are_read_only(tmp_path, service, writable, text):
    note = tmp_path / "Generated.md"
    note.write_text(text)
    workspace = service.open(tmp_path)
    assert workspace["entries"][0]["editable"] is False
    doc = service.read(workspace["id"], "Generated.md")
    assert doc["editable"] is False
    with pytest.raises(DocumentError, match="engine-owned"):
        service.save(workspace["id"], doc["id"], doc["revision"], "remove ownership")
    assert note.read_text() == text


@pytest.mark.parametrize("marker", ["auto-zettelkasten.yml", "workspace_manifest.yml", "EXPORT_MANIFEST.md"])
def test_engine_ancestor_protects_opened_subfolder_and_creation(tmp_path, service, writable, marker):
    root = tmp_path / "workspace"
    notes = root / "02_source_memory" / "notes"
    notes.mkdir(parents=True)
    (root / marker).write_text("engine-owned")
    note = notes / "Plain.md"
    note.write_text("even without metadata")
    workspace = service.open(notes)
    assert workspace["editable"] is False
    assert workspace["entries"][0]["editable"] is False
    doc = service.read(workspace["id"], "Plain.md")
    with pytest.raises(DocumentError, match="engine-owned"):
        service.save(workspace["id"], doc["id"], doc["revision"], "changed")
    for kind in ("file", "directory"):
        with pytest.raises(DocumentError, match="engine-owned"):
            service.create(workspace["id"], "", "New", kind)
    assert note.read_text() == "even without metadata"
    assert not (notes / "New.md").exists()


def test_engine_config_in_ordinary_ancestor_is_rechecked_before_save(tmp_path, service, writable):
    folder = tmp_path / "Notes"
    folder.mkdir()
    note = folder / "Note.md"
    note.write_text("ordinary")
    workspace = service.open(folder)
    doc = service.read(workspace["id"], "Note.md")
    assert doc["editable"] is True
    (tmp_path / "auto-zettelkasten.yml").write_text("new engine ownership")
    with pytest.raises(DocumentError, match="engine-owned"):
        service.save(workspace["id"], doc["id"], doc["revision"], "changed")
    assert note.read_text() == "ordinary"


def test_conflict_contains_current_document_and_preserves_external_bytes(tmp_path, service, writable):
    note = tmp_path / "Note.md"
    note.write_text("original")
    workspace = service.open(tmp_path)
    doc = service.read(workspace["id"], "Note.md")
    note.write_text("external change")
    with pytest.raises(DocumentConflict) as caught:
        service.save(workspace["id"], doc["id"], doc["revision"], "our change")
    assert caught.value.current["text"] == "external change"
    assert caught.value.current["id"] == doc["id"]
    assert note.read_text() == "external change"


def test_same_bytes_replacement_is_also_a_conflict(tmp_path, service, writable):
    note = tmp_path / "Note.md"
    note.write_text("original")
    workspace = service.open(tmp_path)
    doc = service.read(workspace["id"], "Note.md")
    replacement = tmp_path / "new.tmp"
    replacement.write_text("original")
    replacement.replace(note)
    with pytest.raises(DocumentConflict) as caught:
        service.save(workspace["id"], doc["id"], doc["revision"], "ours")
    assert caught.value.current["revision"] == doc["revision"]
    assert note.read_text() == "original"


def test_writes_are_serialized_and_only_one_same_revision_save_succeeds(tmp_path, service, writable):
    (tmp_path / "Note.md").write_text("start")
    workspace = service.open(tmp_path)
    doc = service.read(workspace["id"], "Note.md")

    def save(value):
        try:
            return service.save(workspace["id"], doc["id"], doc["revision"], value)["text"]
        except DocumentConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(save, ["one", "two"]))
    assert outcomes.count("conflict") == 1
    assert (tmp_path / "Note.md").read_text() in {"one", "two"}


def test_save_rechecks_revision_after_writing_temporary(tmp_path, service, monkeypatch, writable):
    note = tmp_path / "Note.md"
    note.write_text("initial")
    workspace = service.open(tmp_path)
    doc = service.read(workspace["id"], "Note.md")
    real_sync = os.fsync
    changed = False

    def concurrent_change(fd):
        nonlocal changed
        if stat.S_ISREG(os.fstat(fd).st_mode) and not changed:
            changed = True
            note.write_text("external late change")
        return real_sync(fd)

    monkeypatch.setattr(os, "fsync", concurrent_change)
    with pytest.raises(DocumentConflict) as caught:
        service.save(workspace["id"], doc["id"], doc["revision"], "our change")
    assert caught.value.current["text"] == "external late change"
    assert note.read_text() == "external late change"
    assert not list(tmp_path.glob(".az-*.tmp"))


def test_swapped_parent_is_rejected_and_never_writes_symlink_destination(tmp_path, service, monkeypatch, writable):
    root = tmp_path / "root"
    folder = root / "Folder"
    folder.mkdir(parents=True)
    note = folder / "Note.md"
    note.write_text("original")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Note.md").write_text("outside")
    workspace = service.open(root)
    doc = service.read(workspace["id"], "Folder/Note.md")
    real_sync = os.fsync
    changed = False

    def swap_parent(fd):
        nonlocal changed
        if stat.S_ISREG(os.fstat(fd).st_mode) and not changed:
            changed = True
            folder.rename(root / "Moved")
            folder.symlink_to(outside, target_is_directory=True)
        return real_sync(fd)

    monkeypatch.setattr(os, "fsync", swap_parent)
    with pytest.raises(DocumentError):
        service.save(workspace["id"], doc["id"], doc["revision"], "our change")
    assert (outside / "Note.md").read_text() == "outside"
    assert (root / "Moved" / "Note.md").read_text() == "original"
    assert not list((root / "Moved").glob(".az-*.tmp"))
    with pytest.raises(DocumentError):
        service.read(workspace["id"], "Folder/Note.md")


def test_swapped_root_is_rejected_before_read_or_create(tmp_path, service, writable):
    root = tmp_path / "root"
    root.mkdir()
    (root / "Note.md").write_text("original")
    workspace = service.open(root)
    root.rename(tmp_path / "moved")
    root.mkdir()
    (root / "Note.md").write_text("replacement")
    with pytest.raises(DocumentError, match="root changed"):
        service.read(workspace["id"], "Note.md")
    with pytest.raises(DocumentError):
        service.create(workspace["id"], "", "New", "file")
    assert not (root / "New.md").exists()


def test_hard_links_are_never_opened_or_saved(tmp_path, service, writable):
    root = tmp_path / "root"
    root.mkdir()
    note = root / "Note.md"
    note.write_text("original")
    workspace = service.open(root)
    doc = service.read(workspace["id"], "Note.md")
    outside = tmp_path / "outside.md"
    os.link(note, outside)
    assert service.tree(workspace["id"])["entries"] == []
    with pytest.raises(DocumentError, match="non-linked"):
        service.read(workspace["id"], "Note.md")
    with pytest.raises(DocumentError):
        service.save(workspace["id"], doc["id"], doc["revision"], "changed")
    assert outside.read_text() == "original"


def test_encoding_mixed_newlines_and_size_limits_are_explicit(tmp_path, service, writable):
    (tmp_path / "binary.md").write_bytes(b"\xff\xfeN\x00")
    (tmp_path / "mixed.md").write_bytes(b"one\r\ntwo\n")
    (tmp_path / "bare-cr.md").write_bytes(b"one\rtwo\r")
    (tmp_path / "large.md").write_bytes(b"a" * (documents.MAX_FILE_BYTES + 1))
    (tmp_path / "good.md").write_text("good")
    workspace = service.open(tmp_path)
    rows = {row["name"]: row for row in workspace["entries"]}
    assert all(not rows[name]["editable"] for name in ("binary.md", "mixed.md", "bare-cr.md", "large.md"))
    for name in ("binary.md", "large.md"):
        with pytest.raises(DocumentError):
            service.read(workspace["id"], name)
    for name in ("mixed.md", "bare-cr.md"):
        doc = service.read(workspace["id"], name)
        assert doc["newline"] == "mixed"
        with pytest.raises(DocumentError):
            service.save(workspace["id"], doc["id"], doc["revision"], "normalized\n")
    good = service.read(workspace["id"], "good.md")
    for text in ("a\r\nb\n", "\x00", "界" * (documents.MAX_FILE_BYTES // 2), "\ud800"):
        with pytest.raises(DocumentError):
            service.save(workspace["id"], good["id"], good["revision"], text)
    assert (tmp_path / "good.md").read_text() == "good"


def test_creation_is_exclusive_and_case_unicode_collision_safe(tmp_path, service, writable):
    (tmp_path / "Café.md").write_text("keep")
    workspace = service.open(tmp_path)
    for name in ("CAFÉ.md", "Cafe\u0301.md"):
        with pytest.raises(DocumentError, match="already exists"):
            service.create(workspace["id"], "", name, "file")
    with pytest.raises(DocumentError):
        service.create(workspace["id"], "", "Café.md", "directory")
    with pytest.raises(DocumentError):
        service.create(workspace["id"], "", "script.js", "file")
    assert (tmp_path / "Café.md").read_text() == "keep"


def test_exclusive_create_does_not_overwrite_a_concurrent_creation(tmp_path, service, monkeypatch, writable):
    workspace = service.open(tmp_path)
    real_open = os.open
    fired = False

    def concurrent_create(path, flags, *args, **kwargs):
        nonlocal fired
        if path == "New.md" and flags & os.O_CREAT and not fired:
            fired = True
            (tmp_path / "New.md").write_text("external")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", concurrent_create)
    with pytest.raises(DocumentError):
        service.create(workspace["id"], "", "New", "file")
    assert (tmp_path / "New.md").read_text() == "external"


@pytest.mark.parametrize("limit,value", [("MAX_ENTRIES", 2), ("MAX_SCANNED_ENTRIES", 2), ("MAX_TREE_BYTES", 2)])
def test_tree_limits_report_truncation(tmp_path, service, monkeypatch, limit, value):
    for number in range(4):
        (tmp_path / f"Note {number}.md").write_text("note")
    monkeypatch.setattr(documents, limit, value)
    workspace = service.open(tmp_path)
    assert workspace["truncated"] is True
    assert len(workspace["entries"]) <= 2


def test_unsupported_platform_is_browse_only(tmp_path, service, monkeypatch):
    monkeypatch.setattr(documents, "_SAFE_WRITES", False)
    (tmp_path / "Note.md").write_text("original")
    workspace = service.open(tmp_path)
    assert workspace["editable"] is False
    doc = service.read(workspace["id"], "Note.md")
    assert doc["text"] == "original" and doc["editable"] is False
    with pytest.raises(DocumentError, match="platform"):
        service.save(workspace["id"], doc["id"], doc["revision"], "changed")
    with pytest.raises(DocumentError, match="platform"):
        service.create(workspace["id"], "", "New", "directory")
    assert (tmp_path / "Note.md").read_text() == "original"


def test_read_only_fallback_checks_paths_without_directory_handles(tmp_path, service, monkeypatch):
    monkeypatch.setattr(documents, "_DIR_HANDLES", False)
    monkeypatch.setattr(documents, "_SAFE_WRITES", False)
    (tmp_path / "Note.md").write_text("readable")
    workspace = service.open(tmp_path)
    assert service.read(workspace["id"], "Note.md")["text"] == "readable"
    assert workspace["editable"] is False


def test_symlink_swap_during_read_never_returns_outside_content(tmp_path, service, monkeypatch):
    if not documents._DIR_HANDLES:
        pytest.skip("Symlink race test needs descriptor-relative reads")
    root = tmp_path / "root"
    root.mkdir()
    note = root / "Note.md"
    note.write_text("inside")
    outside = tmp_path / "outside.md"
    outside.write_text("outside")
    workspace = service.open(root)
    real_open = os.open

    def swap_before_open(path, flags, *args, **kwargs):
        if path == "Note.md" and not flags & os.O_CREAT:
            note.unlink()
            note.symlink_to(outside)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_before_open)
    with pytest.raises(DocumentError):
        service.read(workspace["id"], "Note.md")
    assert outside.read_text() == "outside"


def test_explicit_readonly_permissions_and_failed_fsync_never_write(tmp_path, service, monkeypatch, writable):
    note = tmp_path / "Note.md"
    note.write_text("original")
    note.chmod(0o444)
    workspace = service.open(tmp_path)
    doc = service.read(workspace["id"], "Note.md")
    assert doc["editable"] is False
    with pytest.raises(DocumentError):
        service.save(workspace["id"], doc["id"], doc["revision"], "changed")
    note.chmod(0o600)
    doc = service.read(workspace["id"], "Note.md")

    def unsupported_sync(fd):
        raise OSError("Directory synchronization unavailable")

    monkeypatch.setattr(os, "fsync", unsupported_sync)
    with pytest.raises(DocumentError):
        service.save(workspace["id"], doc["id"], doc["revision"], "changed")
    with pytest.raises(DocumentError):
        service.create(workspace["id"], "", "New", "file")
    assert note.read_text() == "original"
    assert not (tmp_path / "New.md").exists()


def test_failed_workspace_scan_preserves_the_previous_open_workspace(tmp_path, service, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "Note.md").write_text("original")
    (second / "New.md").write_text("unreadable")
    workspace = service.open(first)
    doc = service.read(workspace["id"], "Note.md")
    original_read = service._read_bytes

    def failed_read(directory, name):
        if name == "New.md":
            raise PermissionError("Cannot read this note")
        return original_read(directory, name)

    monkeypatch.setattr(service, "_read_bytes", failed_read)
    with pytest.raises(DocumentError):
        service.open(second)
    assert service.read(workspace["id"], "Note.md")["id"] == doc["id"]


def test_index_is_confined_and_does_not_expose_private_names(tmp_path, service):
    (tmp_path / "Keep.md").write_bytes(b"\xef\xbb\xbf# Keep\r\n")
    for folder in (".codex", "private", "11_state", "indexes", "01_custody"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "Research details.md").write_text("Private findings")
    (tmp_path / "credentials.md").write_text("API credential")
    outside = tmp_path / "private" / "Research details.md"
    if documents._DIR_HANDLES:
        (tmp_path / "Linked.md").symlink_to(outside)
        (tmp_path / "Linked folder").symlink_to(outside.parent, target_is_directory=True)
        os.link(outside, tmp_path / "Hard link.md")
    workspace = service.open(tmp_path)
    snapshot = service.index(workspace["id"])
    assert snapshot == {
        "documents": [{"path": "Keep.md", "text": "# Keep\r\n",
                       "revision": hashlib.sha256((tmp_path / "Keep.md").read_bytes()).hexdigest(),
                       "editable": documents._SAFE_WRITES, "navigation": False,
                       "source_coverage": {"status": "unknown", "label": "Source coverage unknown"}}],
        "relations": [], "truncated": False, "warnings": [],
        "document_identities": [], "relation_resolution": {"missing": 0, "ambiguous": 0},
        "index_coverage": {"status": "complete", "label": "Markdown indexing complete; source and comparison coverage unknown"},
    }
    assert "Research details" not in json.dumps(snapshot)
    assert "Private findings" not in json.dumps(snapshot)


def _published_memory_indexes(root):
    (root / "auto-zettelkasten.yml").write_text("engine_version: test\n")
    contents = {
        "02_source_memory/notes/target.md": "# Target\n",
        "02_source_memory/indexes/INDEX.md": "# Memory\n\n[[../notes/target]] [[by_topic/INDEX]] [[collections/ABC123/INDEX]]\n",
        "02_source_memory/indexes/CLUSTERS.md": "# Clusters\n",
        "02_source_memory/indexes/by_literature/literature-one.md": "# Literature\n\n[[../../notes/target]]\n",
        "02_source_memory/indexes/by_topic/INDEX.md": "# Topics\n\n[[topic-one]]\n",
        "02_source_memory/indexes/by_topic/topic-one.md": "# Topic\n",
        "02_source_memory/indexes/collections/ABC123/INDEX.md": "# Collection\n\n[[sources-001]] [[relationships-001]]\n",
        "02_source_memory/indexes/collections/ABC123/sources-001.md": "# Sources\n",
        "02_source_memory/indexes/collections/ABC123/sources-1000.md": "# More sources\n",
        "02_source_memory/indexes/collections/ABC123/relationships-001.md": "# Accepted relationships\n\nPublished accepted rationale.\n",
        "02_source_memory/indexes/collections/ABC123/relationships-1000.md": "# More accepted relationships\n",
    }
    for path, text in contents.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    (root / "02_source_memory/indexes/typed_links.yml").write_text(json.dumps({"registry_schema_version": documents.RELATIONSHIP_REGISTRY_SCHEMA_VERSION, "links": []}))
    return contents


def test_published_memory_navigation_is_visible_readonly_and_preferred_without_exposing_state(tmp_path, service, writable):
    contents = _published_memory_indexes(tmp_path)
    private_paths = [
        "02_source_memory/indexes/Private.md", "02_source_memory/indexes/arbitrary.md",
        "02_source_memory/indexes/source_catalogue.yml", "02_source_memory/indexes/by_topic/topic.yml",
        "02_source_memory/indexes/by_topic/private/Leak.md", "02_source_memory/indexes/by_literature/profiles/Leak.md",
        "02_source_memory/indexes/collections/ABC123/Private.md", "02_source_memory/indexes/collections/ABC123/sources-01.md",
        "02_source_memory/indexes/collections/ABC123/relationships-01.md", "02_source_memory/indexes/collections/ABC123/nested/Leak.md",
        "02_source_memory/indexes/collections/private/INDEX.md", "02_source_memory/indexes/source_sets/internal.yml",
        "11_state/Leak.md", "indexes/INDEX.md", "Indexes/INDEX.md",
    ]
    for path in private_paths:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("Not published navigation")
    workspace = service.open(tmp_path)
    files = {row["path"]: row for row in workspace["entries"] if row["kind"] == "file"}
    assert set(files) == set(contents)
    assert all(not row["editable"] for row in workspace["entries"])
    assert workspace["preferred_path"] == "02_source_memory/indexes/INDEX.md"
    for path, text in contents.items():
        document = service.read(workspace["id"], path)
        assert document["text"] == text
        assert document["editable"] is False
        with pytest.raises(DocumentError):
            service.save(workspace["id"], document["id"], document["revision"], text + "Changed")
    for path in [*private_paths, "02_source_memory/indexes/typed_links.yml"]:
        with pytest.raises(DocumentError):
            service.read(workspace["id"], path)
    snapshot = service.index(workspace["id"])
    assert {row["path"] for row in snapshot["documents"]} == set(contents)
    assert all(not row["editable"] for row in snapshot["documents"])
    assert "Published accepted rationale" in json.dumps(snapshot)
    assert "Not published navigation" not in json.dumps(snapshot)
    with pytest.raises(DocumentError):
        service.create(workspace["id"], "02_source_memory/indexes", "New", "file")
    with pytest.raises(DocumentError):
        service.prepare_relocation(workspace["id"], "02_source_memory/indexes/INDEX.md", "Moved.md")
    assert {path: (tmp_path / path).read_text() for path in contents} == contents


@pytest.mark.parametrize("marker_kind", ["missing", "directory", "symlink", "hardlink"])
def test_published_index_exception_requires_a_regular_nonlinked_root_marker(tmp_path, service, marker_kind):
    contents = _published_memory_indexes(tmp_path)
    marker = tmp_path / "auto-zettelkasten.yml"
    marker.unlink()
    if marker_kind == "directory":
        marker.mkdir()
    elif marker_kind in {"symlink", "hardlink"}:
        if not documents._DIR_HANDLES:
            pytest.skip("Link protections require descriptor-relative filesystem support")
        target = tmp_path / "marker-target.yml"
        target.write_text("engine_version: test\n")
        marker.symlink_to(target) if marker_kind == "symlink" else os.link(target, marker)
    workspace = service.open(tmp_path)
    assert "preferred_path" not in workspace
    assert not any(row["path"].startswith("02_source_memory/indexes") for row in workspace["entries"])
    with pytest.raises(DocumentError):
        service.read(workspace["id"], "02_source_memory/indexes/INDEX.md")
    assert {row["path"] for row in service.index(workspace["id"])["documents"]} == {"02_source_memory/notes/target.md"}
    assert (tmp_path / "02_source_memory/indexes/INDEX.md").read_text() == contents["02_source_memory/indexes/INDEX.md"]


def test_published_index_marker_is_rechecked_and_linked_files_or_prefixes_stay_excluded(tmp_path, service):
    _published_memory_indexes(tmp_path)
    if not documents._DIR_HANDLES:
        pytest.skip("Link protections require descriptor-relative filesystem support")
    index_root = tmp_path / "02_source_memory/indexes"
    target = tmp_path / "02_source_memory/notes/target.md"
    (index_root / "by_topic/symlink.md").symlink_to(target)
    os.link(target, index_root / "by_topic/hardlink.md")
    (index_root / "collections/LINK").symlink_to(index_root / "collections/ABC123", target_is_directory=True)
    (index_root / "by_topic/directory.md").mkdir()
    workspace = service.open(tmp_path)
    assert not any(row["path"].endswith(("symlink.md", "hardlink.md", "directory.md", "/LINK")) for row in workspace["entries"])
    for path in ["by_topic/symlink.md", "by_topic/hardlink.md", "collections/LINK/INDEX.md"]:
        with pytest.raises(DocumentError):
            service.read(workspace["id"], "02_source_memory/indexes/" + path)
    (tmp_path / "auto-zettelkasten.yml").unlink()
    with pytest.raises(DocumentError):
        service.read(workspace["id"], "02_source_memory/indexes/INDEX.md")
    assert "preferred_path" not in service.tree(workspace["id"])


def test_index_refreshes_additions_edits_and_removals_without_reopening(tmp_path, service):
    note = tmp_path / "One.md"
    note.write_text("original")
    workspace = service.open(tmp_path)
    first = service.index(workspace["id"])
    note.write_text("external edit")
    (tmp_path / "Notes").mkdir()
    (tmp_path / "Notes" / "Two.md").write_text("new note")
    second = service.index(workspace["id"])
    assert [(row["path"], row["text"]) for row in second["documents"]] == [("Notes/Two.md", "new note"), ("One.md", "external edit")]
    assert first["documents"][0]["revision"] != second["documents"][1]["revision"]
    note.unlink()
    assert [row["path"] for row in service.index(workspace["id"])["documents"]] == ["Notes/Two.md"]


def test_index_does_not_reset_the_identity_of_an_open_save_handle(tmp_path, service, writable):
    note = tmp_path / "Note.md"
    note.write_text("original")
    workspace = service.open(tmp_path)
    opened = service.read(workspace["id"], "Note.md")
    replacement = tmp_path / "new.tmp"
    replacement.write_text("original")
    replacement.replace(note)
    assert service.index(workspace["id"])["documents"][0]["text"] == "original"
    with pytest.raises(DocumentConflict):
        service.save(workspace["id"], opened["id"], opened["revision"], "new edit")


@pytest.mark.parametrize("limit,value", [("MAX_ENTRIES", 2), ("MAX_SCANNED_ENTRIES", 2), ("MAX_TREE_BYTES", 7)])
def test_index_limits_report_partial_results(tmp_path, service, monkeypatch, limit, value):
    for number in range(4):
        (tmp_path / f"Note {number}.md").write_text("note")
    workspace = service.open(tmp_path)
    monkeypatch.setattr(documents, limit, value)
    snapshot = service.index(workspace["id"])
    assert snapshot["truncated"] is True
    assert len(snapshot["documents"]) <= 2
    assert sum(len(row["text"].encode()) for row in snapshot["documents"]) <= documents.MAX_TREE_BYTES
    assert "partial" in snapshot["warnings"][0]


def test_index_skips_unreadable_invalid_or_oversize_notes(tmp_path, service, monkeypatch):
    for name in ("Good.md", "Changed.md", "Large.md", "Binary.md"):
        (tmp_path / name).write_text("note")
    workspace = service.open(tmp_path)
    (tmp_path / "Large.md").write_bytes(b"x" * (documents.MAX_FILE_BYTES + 1))
    (tmp_path / "Binary.md").write_bytes(b"\xff")
    original = service._read_bytes

    def unreadable(directory, name):
        if name == "Changed.md":
            raise PermissionError("Private diagnostic with a sensitive absolute path")
        return original(directory, name)

    monkeypatch.setattr(service, "_read_bytes", unreadable)
    snapshot = service.index(workspace["id"])
    assert [row["path"] for row in snapshot["documents"]] == ["Good.md"]
    assert snapshot["warnings"] and not snapshot["truncated"]
    assert "sensitive" not in json.dumps(snapshot)
    assert "Changed.md" not in json.dumps(snapshot)


def test_index_symlink_race_never_reads_an_outside_note(tmp_path, service, monkeypatch):
    if not documents._DIR_HANDLES:
        pytest.skip("Symlink race test needs descriptor-relative reads")
    root = tmp_path / "notes"
    root.mkdir()
    note = root / "Note.md"
    note.write_text("original")
    outside = tmp_path / "Outside.md"
    outside.write_text("outside text")
    workspace = service.open(root)
    original = os.open

    def swap(path, flags, *args, **kwargs):
        if path == "Note.md" and not flags & os.O_CREAT:
            note.unlink()
            note.symlink_to(outside)
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap)
    snapshot = service.index(workspace["id"])
    assert snapshot["documents"] == [] and snapshot["warnings"]
    assert "outside text" not in json.dumps(snapshot)


def _typed_workspace(root, rows):
    (root / "auto-zettelkasten.yml").write_text("engine_version: test\n")
    notes = root / "02_source_memory" / "notes"
    notes.mkdir(parents=True)
    (notes / "One.md").write_text("---\nnote_id: note-one\n---\n# One\n")
    (notes / "Two.md").write_text("---\nnote_id: note-two\n---\n# Two\n")
    registry = root / "02_source_memory" / "indexes" / "typed_links.yml"
    registry.parent.mkdir()
    registry.write_text(json.dumps({"registry_schema_version": "7", "links": rows}))
    return notes, registry


def _typed_row(relation_id="relation-1", **updates):
    return {"relation_id": relation_id, "source_note_id": "note-one", "target_note_id": "note-two",
            "relation_type": "supports", "active": True, "decision_status": "accepted", **updates}


def test_index_projects_only_active_accepted_published_typed_relations(tmp_path, service):
    rows = [
        _typed_row(), _typed_row("inactive", active=False),
        _typed_row("pending", decision_status="reconciliation_pending"),
        _typed_row("refresh", refresh_pending=True),
        _typed_row("reconciliation", reconciliation_pending=True),
        _typed_row("superseded", decision_status="superseded"),
        _typed_row("outside", target_note_id="outside-note"),
        _typed_row("details", relation_type="cites", reason="private rationale", evidence=[{"private": "metadata"}]),
    ]
    _, registry = _typed_workspace(tmp_path, rows)
    value = json.loads(registry.read_text())
    value["relations"] = [_typed_row("unpublished")]
    registry.write_text(json.dumps(value))
    original = registry.read_bytes()
    workspace = service.open(tmp_path)
    snapshot = service.index(workspace["id"])
    assert snapshot["relations"] == [
        {"id": "details", "source": "02_source_memory/notes/One.md", "target": "02_source_memory/notes/Two.md", "type": "cites"},
        {"id": "relation-1", "source": "02_source_memory/notes/One.md", "target": "02_source_memory/notes/Two.md", "type": "supports"},
    ]
    assert all(not row["editable"] for row in snapshot["documents"])
    assert snapshot["relation_resolution"] == {"missing": 1, "ambiguous": 0}
    assert snapshot["warnings"] == ["Some typed relations have missing note destinations."]
    assert snapshot["index_coverage"]["status"] == "complete"
    assert "private rationale" not in json.dumps(snapshot)
    assert "metadata" not in json.dumps(snapshot)
    assert registry.read_bytes() == original
    with pytest.raises(DocumentError):
        service.read(workspace["id"], "02_source_memory/indexes/typed_links.yml")


def test_index_never_uses_an_ancestor_or_compatibility_registry(tmp_path, service):
    notes, registry = _typed_workspace(tmp_path, [_typed_row()])
    workspace = service.open(notes)
    nested = service.index(workspace["id"])
    assert nested["relations"] == [] and nested["warnings"]
    registry.rename(registry.with_name("typed_note_links.yml"))
    workspace = service.open(tmp_path)
    missing = service.index(workspace["id"])
    assert missing["relations"] == [] and missing["warnings"]


@pytest.mark.parametrize("content", ["[", "null", '{"registry_schema_version":"6","links":[]}', '{"registry_schema_version":"7","links":{}}'])
def test_index_rejects_malformed_or_unsupported_typed_registry(tmp_path, service, content):
    _, registry = _typed_workspace(tmp_path, [])
    registry.write_text(content)
    workspace = service.open(tmp_path)
    snapshot = service.index(workspace["id"])
    assert len(snapshot["documents"]) == 2
    assert snapshot["relations"] == [] and snapshot["warnings"]
    assert str(registry) not in json.dumps(snapshot)


def test_index_does_not_guess_between_duplicate_note_ids(tmp_path, service):
    notes, _ = _typed_workspace(tmp_path, [_typed_row()])
    (notes / "Copy.md").write_text("---\nnote_id: note-one\n---\ncopy")
    workspace = service.open(tmp_path)
    assert service.index(workspace["id"])["relations"] == []


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink", "parent-symlink", "oversize"])
def test_index_confines_the_exact_special_registry_read(tmp_path, service, link_kind):
    if not documents._DIR_HANDLES and link_kind in {"symlink", "parent-symlink"}:
        pytest.skip("Symlink test needs descriptor-relative reads")
    root = tmp_path / "workspace"
    root.mkdir()
    _, registry = _typed_workspace(root, [_typed_row()])
    outside = tmp_path / "Outside.yml"
    registry.rename(outside)
    if link_kind == "symlink":
        registry.symlink_to(outside)
    elif link_kind == "hardlink":
        os.link(outside, registry)
    elif link_kind == "parent-symlink":
        registry.parent.rmdir()
        registry.parent.symlink_to(tmp_path, target_is_directory=True)
        outside.rename(tmp_path / "typed_links.yml")
    else:
        registry.write_bytes(b"x" * (documents.MAX_FILE_BYTES + 1))
    workspace = service.open(root)
    snapshot = service.index(workspace["id"])
    assert snapshot["relations"] == [] and snapshot["warnings"]


def test_index_skips_invalid_relation_records_and_rejects_conflicting_ids(tmp_path, service):
    _, registry = _typed_workspace(tmp_path, [42, _typed_row("unsafe/path"), _typed_row("unknown", relation_type="unknown"), _typed_row()])
    workspace = service.open(tmp_path)
    snapshot = service.index(workspace["id"])
    assert len(snapshot["relations"]) == 1 and snapshot["warnings"]
    registry.write_text(json.dumps({"registry_schema_version": "7", "links": [_typed_row(), _typed_row(relation_type="cites")]}))
    snapshot = service.index(workspace["id"])
    assert snapshot["relations"] == [] and snapshot["warnings"]


def _repair(preview, path, before, after):
    document = next(row for row in preview["documents"] if row["path"] == path)
    text = document["text"].replace("\r\n", "\n")
    offset = text.index(before)
    start = len(text[:offset].encode("utf-16-le")) // 2
    return {"path": path, "revision": document["revision"],
            "edits": [{"from": start, "to": start + len(before.encode("utf-16-le")) // 2, "before": before, "after": after}]}


def test_relocation_preserves_bom_crlf_unicode_modes_handles_and_idempotent_receipt(tmp_path, service, writable):
    (tmp_path / "Notes").mkdir()
    (tmp_path / "Drafts").mkdir()
    raw = b"\xef\xbb\xbf" + "# Authored title\r\n\r\nEmoji \U0001f4dd before [[Notes/Target#Heading|Name]].\r\n".encode()
    source = tmp_path / "Source.md"
    source.write_bytes(raw)
    source.chmod(0o640)
    target = tmp_path / "Notes" / "Target.md"
    target.write_text("# Heading\n\nParagraph ^block\n")
    workspace = service.open(tmp_path)
    opened = service.read(workspace["id"], "Notes/Target.md")
    preview = service.prepare_relocation(workspace["id"], "Notes/Target.md", "Drafts/Renamed.md")
    assert all(len(row["recovery_key"]) == 64 for row in preview["documents"])
    assert len(preview["moves"][0]["target_recovery_key"]) == 64
    assert target.exists() and not (tmp_path / "Drafts" / "Renamed.md").exists()
    patch = _repair(preview, "Source.md", "Notes/Target#Heading", "Drafts/Renamed#Heading")
    result = service.apply_relocation(workspace["id"], preview["token"], True, [patch])
    assert source.read_bytes() == raw.replace(b"Notes/Target", b"Drafts/Renamed")
    assert stat.S_IMODE(source.stat().st_mode) == 0o640
    moved = next(row for row in result["documents"] if row["path"] == "Drafts/Renamed.md")
    assert moved["id"] == opened["id"] and moved["text"].startswith("# Heading")
    assert {"name", "reason", "bom", "newline", "recovery_key"} <= moved.keys()
    target.write_text("recreated old filename")
    service.save(workspace["id"], moved["id"], moved["revision"], "changed logical moved note")
    assert target.read_text() == "recreated old filename"
    assert (tmp_path / "Drafts" / "Renamed.md").read_text() == "changed logical moved note"
    assert service.apply_relocation(workspace["id"], preview["token"], True, [patch]) == result


def test_folder_and_empty_folder_moves_preserve_membership_and_unique_links(tmp_path, service, writable):
    (tmp_path / "Notes" / "Nested").mkdir(parents=True)
    (tmp_path / "Notes" / "Nested" / "Target.md").write_text("# Keep authored title")
    (tmp_path / "Source.md").write_text("[[Target]]")
    workspace = service.open(tmp_path)
    preview = service.prepare_relocation(workspace["id"], "Notes", "Drafts")
    result = service.apply_relocation(workspace["id"], preview["token"], True, [])
    assert result["moves"][0]["to"] == "Drafts/Nested/Target.md"
    assert (tmp_path / "Source.md").read_text() == "[[Target]]"
    assert (tmp_path / "Drafts" / "Nested" / "Target.md").read_text() == "# Keep authored title"
    (tmp_path / "Empty").mkdir()
    preview = service.prepare_relocation(workspace["id"], "Empty", "Renamed empty")
    assert preview["moves"] == []
    result = service.apply_relocation(workspace["id"], preview["token"], False, [])
    assert result["documents"] == [] and (tmp_path / "Renamed empty").is_dir()


@pytest.mark.parametrize("destination", ["Note.md", "NOTE.md", "Cafe\u0301.md", "private/Note.md", "02_source_memory/Note.md", "not-markdown.txt", "../Outside.md"])
def test_relocation_rejects_self_case_unicode_collisions_and_unsafe_destinations(tmp_path, service, writable, destination):
    (tmp_path / "Note.md").write_text("note")
    (tmp_path / "Caf\u00e9.md").write_text("keep")
    workspace = service.open(tmp_path)
    with pytest.raises(DocumentError):
        service.prepare_relocation(workspace["id"], "Note.md", destination)
    assert (tmp_path / "Note.md").read_text() == "note"


@pytest.mark.parametrize("entry", [".hidden", "attachment.pdf", "private", "engine.md", "link.md"])
def test_source_folder_must_contain_only_ordinary_markdown(tmp_path, service, writable, entry):
    folder = tmp_path / "Notes"
    folder.mkdir()
    (folder / "Ordinary.md").write_text("ordinary")
    if entry == "private":
        (folder / entry).mkdir()
    elif entry == "engine.md":
        (folder / entry).write_text("---\nnote_id: owned\n---\nengine")
    elif entry == "link.md":
        (folder / entry).symlink_to(folder / "Ordinary.md")
    else:
        (folder / entry).write_text("unmanaged")
    workspace = service.open(tmp_path)
    with pytest.raises(DocumentError):
        service.prepare_relocation(workspace["id"], "Notes", "Renamed")
    assert folder.exists()


def test_relocation_rejects_descendants_generated_roots_and_new_engine_directory_names(tmp_path, service, writable):
    (tmp_path / "Notes" / "Nested").mkdir(parents=True)
    (tmp_path / "Notes" / "Plain.md").write_text("plain")
    workspace = service.open(tmp_path)
    for destination in ("Notes/Nested/Renamed", "02_source_memory", "03_literature_synthesis"):
        with pytest.raises(DocumentError):
            service.prepare_relocation(workspace["id"], "Notes", destination)
    (tmp_path / "auto-zettelkasten.yml").write_text("engine_version: test")
    with pytest.raises(DocumentError):
        service.prepare_relocation(workspace["id"], "Notes", "Renamed")


@pytest.mark.parametrize("change", ["addition", "edit", "remove", "unknown", "marker", "permission"])
def test_relocation_rechecks_the_entire_reviewed_workspace(tmp_path, service, writable, change):
    (tmp_path / "Note.md").write_text("note")
    other = tmp_path / "Other.md"
    other.write_text("[[Note]]")
    workspace = service.open(tmp_path)
    preview = service.prepare_relocation(workspace["id"], "Note.md", "Renamed.md")
    if change == "addition":
        (tmp_path / "New.md").write_text("new ambiguity")
    elif change == "edit":
        other.write_text("changed")
    elif change == "remove":
        other.unlink()
    elif change == "unknown":
        (tmp_path / ".new-hidden").write_text("unmanaged")
    elif change == "marker":
        (tmp_path / "auto-zettelkasten.yml").write_text("engine_version: test")
    else:
        other.chmod(0o444)
    with pytest.raises(DocumentError):
        service.apply_relocation(workspace["id"], preview["token"], False, [])
    assert (tmp_path / "Note.md").read_text() == "note"
    assert not (tmp_path / "Renamed.md").exists()


@pytest.mark.parametrize("invalid", ["revision", "before", "overlap", "surrogate", "newline", "protected", "declined"])
def test_reference_repairs_validate_ranges_revisions_consent_and_protection(tmp_path, service, writable, invalid):
    (tmp_path / "Target.md").write_text("target")
    (tmp_path / "Source.md").write_text("\U0001f4dd [[Target]]")
    (tmp_path / "Generated.md").write_text("---\nnote_id: protected\n---\n[[Target]]")
    workspace = service.open(tmp_path)
    preview = service.prepare_relocation(workspace["id"], "Target.md", "Renamed.md")
    patch = _repair(preview, "Generated.md" if invalid == "protected" else "Source.md", "Target", "Renamed")
    if invalid == "revision":
        patch["revision"] = "0" * 64
    elif invalid == "before":
        patch["edits"][0]["before"] = "wrong"
    elif invalid == "overlap":
        patch["edits"].append(dict(patch["edits"][0]))
    elif invalid == "surrogate":
        patch["edits"] = [{"from": 1, "to": 2, "before": "x", "after": "y"}]
    elif invalid == "newline":
        patch["edits"][0]["after"] += "\nnew line"
    with pytest.raises(DocumentError):
        service.apply_relocation(workspace["id"], preview["token"], invalid != "declined", [patch])
    assert (tmp_path / "Target.md").read_text() == "target"


def test_relocation_rolls_back_mid_repair_failure_including_replaced_source_inode(tmp_path, service, writable, monkeypatch):
    (tmp_path / "Note.md").write_text("[[Note]]\n")
    (tmp_path / "Source.md").write_text("[[Note]]\n")
    workspace = service.open(tmp_path)
    preview = service.prepare_relocation(workspace["id"], "Note.md", "Renamed.md")
    patches = [_repair(preview, path, "Note", "Renamed") for path in ("Note.md", "Source.md")]
    relocator = service._relocations()
    original = relocator._install

    def fail_after_source(*args, **kwargs):
        result = original(*args, **kwargs)
        if not kwargs.get("rollback"):
            raise OSError("simulated mid-operation failure")
        return result

    monkeypatch.setattr(relocator, "_install", fail_after_source)
    with pytest.raises(DocumentError, match="Original notes and paths were restored"):
        service.apply_relocation(workspace["id"], preview["token"], True, patches)
    assert (tmp_path / "Note.md").read_text() == "[[Note]]\n"
    assert (tmp_path / "Source.md").read_text() == "[[Note]]\n"
    assert not (tmp_path / "Renamed.md").exists()
    assert not relocator.blocked


def test_relocation_crash_recovery_is_idempotent_before_later_writes(tmp_path, service, writable, monkeypatch):
    class Crash(BaseException):
        pass

    (tmp_path / "Note.md").write_text("[[Note]]\n")
    workspace = service.open(tmp_path)
    preview = service.prepare_relocation(workspace["id"], "Note.md", "Renamed.md")
    relocator = service._relocations()
    original = relocator._install

    def crash_after_write(*args, **kwargs):
        original(*args, **kwargs)
        raise Crash()

    monkeypatch.setattr(relocator, "_install", crash_after_write)
    with pytest.raises(Crash):
        service.apply_relocation(workspace["id"], preview["token"], True, [_repair(preview, "Note.md", "Note", "Renamed")])
    assert (tmp_path / "Renamed.md").exists()
    restarted = DocumentService(journal_root=service._journal_root)
    try:
        reopened = restarted.open(tmp_path)
        assert (tmp_path / "Note.md").read_text() == "[[Note]]\n"
        assert not (tmp_path / "Renamed.md").exists()
        restarted._relocations().recover()
        assert not restarted._relocations().blocked
        assert restarted.create(reopened["id"], "", "After recovery", "file")["document"]["path"] == "After recovery.md"
    finally:
        restarted.close()


@pytest.mark.parametrize("external", ["edited", "new-file", "source-collision"])
def test_recovery_preserves_external_changes_and_blocks_all_mutations(tmp_path, service, writable, monkeypatch, external):
    (tmp_path / "Notes").mkdir()
    (tmp_path / "Notes" / "Note.md").write_text("original")
    workspace = service.open(tmp_path)
    doc = service.read(workspace["id"], "Notes/Note.md")
    preview = service.prepare_relocation(workspace["id"], "Notes", "Drafts")
    relocator = service._relocations()
    original = relocator._move

    def concurrent_change(source, destination, identity):
        original(source, destination, identity)
        if source == "Notes":
            if external == "edited":
                (tmp_path / "Drafts" / "Note.md").write_text("external edit")
            elif external == "new-file":
                (tmp_path / "Drafts" / "Arrived.md").write_text("external addition")
            else:
                (tmp_path / "Notes").mkdir()
                (tmp_path / "Notes" / "Keep.md").write_text("source collision")
            raise OSError("interruption")

    monkeypatch.setattr(relocator, "_move", concurrent_change)
    with pytest.raises(DocumentError, match="recovery needs review"):
        service.apply_relocation(workspace["id"], preview["token"], False, [])
    assert (tmp_path / "Drafts" / "Note.md").exists()
    assert any(service._journal_root.rglob("journal.json"))
    with pytest.raises(DocumentError):
        service.create(workspace["id"], "", "No new writes", "file")
    with pytest.raises(DocumentError):
        service.save(workspace["id"], doc["id"], doc["revision"], "must not overwrite")
    reopened = service.open(tmp_path)
    assert not reopened["editable"]
    if external == "edited":
        assert (tmp_path / "Drafts" / "Note.md").read_text() == "external edit"
    elif external == "new-file":
        assert (tmp_path / "Drafts" / "Arrived.md").read_text() == "external addition"
    else:
        assert (tmp_path / "Notes" / "Keep.md").read_text() == "source collision"


def test_exclusive_relocation_refuses_raced_destination_and_cross_device_failure(tmp_path, service, writable, monkeypatch):
    from auto_zettelkasten import organization

    (tmp_path / "Note.md").write_text("original")
    workspace = service.open(tmp_path)
    preview = service.prepare_relocation(workspace["id"], "Note.md", "Renamed.md")
    original = organization._exclusive_rename

    def raced_destination(origin_fd, source, target_fd, destination):
        (tmp_path / "Renamed.md").write_text("external destination")
        return original(origin_fd, source, target_fd, destination)

    monkeypatch.setattr(organization, "_exclusive_rename", raced_destination)
    with pytest.raises(DocumentError):
        service.apply_relocation(workspace["id"], preview["token"], False, [])
    assert (tmp_path / "Note.md").read_text() == "original"
    assert (tmp_path / "Renamed.md").read_text() == "external destination"
    # A failed exclusive rename never touches either existing file. Its journal is
    # retained because both locations now exist and cannot be safely conflated.
    assert service._relocations().blocked


def test_relocation_journal_is_private_confined_and_lock_coordinates_app_instances(tmp_path, service, writable):
    from auto_zettelkasten.documents import _LOCK

    (tmp_path / "Note.md").write_text("original")
    workspace = service.open(tmp_path)
    relocator = service._relocations()
    second = DocumentService(journal_root=service._journal_root)
    try:
        with _LOCK, relocator.guard(workspace["id"]):
            other = second.open(tmp_path)
            assert "Another app" in other["reason"]
            with pytest.raises(DocumentError, match="Another app"):
                second.create(other["id"], "", "Blocked", "file")
        assert second.prepare_relocation(other["id"], "Note.md", "Renamed.md")["source"] == "Note.md"
        assert stat.S_IMODE(service._journal_root.stat().st_mode) == 0o700
    finally:
        second.close()
    inside = DocumentService(journal_root=tmp_path / "recovery")
    try:
        selected = inside.open(tmp_path)
        with pytest.raises(DocumentError, match="outside the app"):
            inside.prepare_relocation(selected["id"], "Note.md", "Renamed.md")
    finally:
        inside.close()


def test_relocation_cross_device_error_restores_without_overwriting(tmp_path, service, writable, monkeypatch):
    import errno
    from auto_zettelkasten import organization

    (tmp_path / "Note.md").write_text("original")
    workspace = service.open(tmp_path)
    preview = service.prepare_relocation(workspace["id"], "Note.md", "Renamed.md")

    def cross_device(*args):
        raise OSError(errno.EXDEV, "Different devices")

    monkeypatch.setattr(organization, "_exclusive_rename", cross_device)
    with pytest.raises(DocumentError, match="Original notes and paths were restored"):
        service.apply_relocation(workspace["id"], preview["token"], False, [])
    assert (tmp_path / "Note.md").read_text() == "original"
    assert not (tmp_path / "Renamed.md").exists()
    assert not service._relocations().blocked
    assert not list(service._journal_root.rglob("journal.json"))


def test_committed_operation_survives_response_failure_retry_and_restart(tmp_path, service, writable, monkeypatch):
    (tmp_path / "Note.md").write_text("original")
    workspace = service.open(tmp_path)
    preview = service.prepare_relocation(workspace["id"], "Note.md", "Renamed.md")
    original_read = service.read

    def response_failure(*args):
        raise DocumentError("Simulated response generation failure")

    monkeypatch.setattr(service, "read", response_failure)
    with pytest.raises(DocumentError, match="response generation"):
        service.apply_relocation(workspace["id"], preview["token"], False, [])
    assert (tmp_path / "Renamed.md").read_text() == "original" and not (tmp_path / "Note.md").exists()
    monkeypatch.setattr(service, "read", original_read)
    result = service.apply_relocation(workspace["id"], preview["token"], False, [])
    assert result["documents"][0]["path"] == "Renamed.md"
    service.close()
    reopened = service.open(tmp_path)
    assert [row["path"] for row in reopened["entries"]] == ["Renamed.md"]


def test_preparation_backup_failure_leaves_notes_unchanged_and_recovers(tmp_path, service, writable, monkeypatch):
    from auto_zettelkasten import organization

    (tmp_path / "Note.md").write_text("original")
    workspace = service.open(tmp_path)
    preview = service.prepare_relocation(workspace["id"], "Note.md", "Renamed.md")
    original_write = organization._write_all

    def backup_failure(fd, raw):
        if raw == b"original":
            raise OSError("Simulated private backup failure")
        return original_write(fd, raw)

    monkeypatch.setattr(organization, "_write_all", backup_failure)
    with pytest.raises(DocumentError):
        service.apply_relocation(workspace["id"], preview["token"], False, [])
    assert (tmp_path / "Note.md").read_text() == "original"
    assert not (tmp_path / "Renamed.md").exists()
    assert not service._relocations().blocked


def test_journal_symlinks_and_unreadable_partial_snapshot_refuse_organization(tmp_path, service, writable, monkeypatch):
    (tmp_path / "Note.md").write_text("original")
    workspace = service.open(tmp_path)
    original_index = service.index

    def partial(*args):
        value = original_index(*args)
        value["truncated"] = True
        return value

    monkeypatch.setattr(service, "index", partial)
    with pytest.raises(DocumentError, match="complete readable"):
        service.prepare_relocation(workspace["id"], "Note.md", "Renamed.md")
    outside = tmp_path.parent / (tmp_path.name + "-external-journals")
    outside.mkdir()
    linked_store = tmp_path.parent / (tmp_path.name + "-linked-journals")
    linked_store.symlink_to(outside, target_is_directory=True)
    linked = DocumentService(journal_root=linked_store)
    try:
        opened = linked.open(tmp_path)
        with pytest.raises(DocumentError):
            linked.prepare_relocation(opened["id"], "Note.md", "Renamed.md")
        assert list(outside.iterdir()) == []
    finally:
        linked.close()


def test_preview_tokens_are_single_workspace_bounded_and_superseded(tmp_path, service, writable):
    (tmp_path / "Note.md").write_text("original")
    workspace = service.open(tmp_path)
    old = service.prepare_relocation(workspace["id"], "Note.md", "Renamed.md")
    new = service.prepare_relocation(workspace["id"], "Note.md", "Other.md")
    assert len(service._relocations().previews) == 1
    with pytest.raises(DocumentError, match="expired"):
        service.apply_relocation(workspace["id"], old["token"], False, [])
    reopened = service.open(tmp_path)
    with pytest.raises(DocumentError, match="expired"):
        service.apply_relocation(reopened["id"], new["token"], False, [])


@pytest.mark.parametrize("corruption", ["temporary", "identity", "subtree", "mode", "committed"])
def test_corrupt_recovery_journal_preserves_browsing_and_blocks_writes(tmp_path, service, writable, corruption):
    (tmp_path / "Note.md").write_text("original")
    workspace = service.open(tmp_path)
    preview = service.prepare_relocation(workspace["id"], "Note.md", "Renamed.md")
    relocator = service._relocations()
    with relocator.guard(workspace["id"]):
        journal = relocator._journal_create(relocator.previews[preview["token"]], {})
        if corruption == "temporary":
            journal["records"][0]["temporary"] = {"path": 0, "identity": None}
        elif corruption == "identity":
            journal["records"][0]["after_identity"] = [True, 2, 3]
        elif corruption == "subtree":
            journal["subtree"]["Note.md"]["identity"] = "invalid"
        elif corruption == "mode":
            journal["records"][0]["mode"] = "0644"
        else:
            journal["committed"] = "false"
        relocator._journal_write(journal)
    reopened = service.open(tmp_path)
    assert reopened["editable"] is False and "recovery needs review" in reopened["reason"]
    doc = service.read(reopened["id"], "Note.md")
    assert doc["text"] == "original"
    with pytest.raises(DocumentError):
        service.save(reopened["id"], doc["id"], doc["revision"], "not saved")
    assert any(service._journal_root.rglob("journal.json"))


def test_case_alias_roots_share_locks_and_cannot_hide_journals_inside_workspace(tmp_path, service, writable):
    alias = tmp_path.with_name(tmp_path.name.swapcase())
    try:
        same = alias.stat().st_ino == tmp_path.stat().st_ino and alias != tmp_path
    except FileNotFoundError:
        same = False
    if not same:
        pytest.skip("Case aliases require a case-insensitive volume")
    (tmp_path / "Note.md").write_text("original")
    workspace = service.open(tmp_path)
    other = DocumentService(journal_root=service._journal_root)
    try:
        with service._relocations().guard(workspace["id"]):
            opened = other.open(alias)
            assert "Another app" in opened["reason"]
        assert other._relocations()._store() == service._relocations()._store()
    finally:
        other.close()
    inside_alias = DocumentService(journal_root=alias / "Recovery data")
    try:
        opened = inside_alias.open(tmp_path)
        with pytest.raises(DocumentError, match="outside the app"):
            inside_alias.prepare_relocation(opened["id"], "Note.md", "Renamed.md")
        assert not (tmp_path / "Recovery data").exists()
    finally:
        inside_alias.close()
