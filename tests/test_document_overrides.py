from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path

import pytest

from auto_zettelkasten import documents
from auto_zettelkasten.documents import DocumentConflict, DocumentError, DocumentIndexChanged, DocumentService


pytestmark = pytest.mark.skipif(not documents._SAFE_WRITES, reason="Safe private overrides require directory handles")
HEADER = "---\nnote_id: note-one\ntitle: Generated\n---\n"


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "auto-zettelkasten.yml").write_text("version: 1\n")
    registry = root / "02_source_memory" / "indexes"
    registry.mkdir(parents=True)
    (registry / "typed_links.yml").write_text(json.dumps({"registry_schema_version": documents.RELATIONSHIP_REGISTRY_SCHEMA_VERSION, "links": []}))
    (root / "Note.md").write_text(HEADER + "\nCanonical body.\n")
    service = DocumentService(journal_root=tmp_path / "journal", override_root=tmp_path / "overrides")
    workspace = service.open(root)
    yield root, service, workspace, tmp_path / "overrides"
    service.close()


def saved(setup, body="\nMy body with [[Target]].\n"):
    root, service, workspace, store = setup
    note = service.read(workspace["id"], "Note.md")
    result = service.save_override(workspace["id"], note["id"], note["revision"], HEADER + body)
    return result


def pages(service, workspace, **kwargs):
    result = []
    page = service.index_page(workspace["id"], **kwargs)
    result.append(page)
    while not page["complete"]:
        page = service.index_page(workspace["id"], cursor=page["next_cursor"])
        result.append(page)
    return result


def test_body_override_persists_without_touching_generated_bytes_or_metadata(setup, tmp_path):
    root, service, workspace, store = setup
    info = (root / "Note.md").stat()
    original = ((root / "Note.md").read_bytes(), info.st_mtime_ns, info.st_ino)
    note = service.read(workspace["id"], "Note.md")
    assert note["override_available"] and not note["editable"] and not note["override_saved"]
    result = saved(setup)
    assert result["override_saved"] and result["override_available"] and not result["source_changed"]
    assert result["text"] == HEADER + "\nMy body with [[Target]].\n"
    assert result["revision"] != result["source_revision"] == hashlib.sha256(original[0]).hexdigest()
    info = (root / "Note.md").stat()
    assert ((root / "Note.md").read_bytes(), info.st_mtime_ns, info.st_ino) == original
    assert not list(root.rglob("*.json"))
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in store.rglob("*.json"))
    reopened = DocumentService(journal_root=tmp_path / "journal2", override_root=store)
    try:
        other_workspace = reopened.open(root)
        assert reopened.read(other_workspace["id"], "Note.md")["text"] == result["text"]
    finally:
        reopened.close()
    with pytest.raises(DocumentError):
        service.save(workspace["id"], result["id"], result["revision"], result["text"])
    with pytest.raises(DocumentError):
        service.prepare_relocation(workspace["id"], "Note.md", "Other.md")


def test_regeneration_preserves_override_body_and_refreshes_properties(setup):
    root, service, workspace, _ = setup
    old = saved(setup)
    regenerated = HEADER.replace("Generated", "Regenerated") + "\nNew upstream body.\n"
    (root / "Note.md").write_text(regenerated)
    current = service.read(workspace["id"], "Note.md")
    assert current["text"] == HEADER.replace("Generated", "Regenerated") + "\nMy body with [[Target]].\n"
    assert current["source_changed"] and current["source_revision"] == hashlib.sha256(regenerated.encode()).hexdigest()
    with pytest.raises(DocumentConflict) as caught:
        service.save_override(workspace["id"], old["id"], old["revision"], old["text"])
    assert caught.value.current["text"] == current["text"]
    rebased = service.save_override(workspace["id"], current["id"], current["revision"], current["text"])
    assert not rebased["source_changed"]
    assert (root / "Note.md").read_text() == regenerated


@pytest.mark.parametrize("text", ["No generated properties", HEADER.replace("note-one", "changed") + "Body", HEADER.replace("Generated", "Changed title") + "Body"])
def test_generated_properties_cannot_be_changed(setup, text):
    root, service, workspace, store = setup
    note = service.read(workspace["id"], "Note.md")
    with pytest.raises(DocumentError, match="Generated properties"):
        service.save_override(workspace["id"], note["id"], note["revision"], text)
    assert not list(store.rglob("*.json"))
    assert (root / "Note.md").read_text() == HEADER + "\nCanonical body.\n"


def test_workspaces_with_identical_note_paths_are_isolated(setup, tmp_path):
    root, service, workspace, store = setup
    first = saved(setup)
    other = tmp_path / "second"
    other.mkdir()
    (other / "auto-zettelkasten.yml").write_text("version: 1")
    (other / "Note.md").write_text(HEADER + "Other source")
    second = service.open(other)
    assert not service.read(second["id"], "Note.md")["override_saved"]
    reopened = service.open(root)
    assert service.read(reopened["id"], "Note.md")["text"] == first["text"]


def test_two_open_services_conflict_instead_of_losing_an_override(setup, tmp_path):
    root, service, workspace, store = setup
    other = DocumentService(journal_root=tmp_path / "journal2", override_root=store)
    second_workspace = other.open(root)
    first = service.read(workspace["id"], "Note.md")
    second = other.read(second_workspace["id"], "Note.md")
    def attempt(current, ws, doc, body):
        try:
            return current.save_override(ws["id"], doc["id"], doc["revision"], HEADER + body)
        except DocumentConflict:
            return "conflict"
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda args: attempt(*args), [(service, workspace, first, "One"), (other, second_workspace, second, "Two")]))
        assert sum(result == "conflict" for result in results) == 1
        assert service.read(workspace["id"], "Note.md")["text"] in [HEADER + "One", HEADER + "Two"]
    finally:
        other.close()


def test_source_change_during_override_write_is_a_conflict(setup, monkeypatch):
    root, service, workspace, store = setup
    note = service.read(workspace["id"], "Note.md")
    real_write = os.write
    changed = False
    def replace_source(fd, data):
        nonlocal changed
        if not changed:
            changed = True
            (root / "Note.md").write_text(HEADER + "External change")
        return real_write(fd, data)
    monkeypatch.setattr(os, "write", replace_source)
    with pytest.raises(DocumentConflict):
        service.save_override(workspace["id"], note["id"], note["revision"], HEADER + "My edit")
    assert not list(store.rglob("*.json"))
    assert (root / "Note.md").read_text() == HEADER + "External change"


def test_override_change_during_write_is_detected(setup, monkeypatch):
    root, service, workspace, store = setup
    note = saved(setup)
    path = next(store.rglob("*.json"))
    real_write = os.write
    changed = False
    def external_override(fd, data):
        nonlocal changed
        if not changed:
            changed = True
            record = json.loads(path.read_text())
            record["body"] = "Other editor"
            path.write_text(json.dumps(record))
        return real_write(fd, data)
    monkeypatch.setattr(os, "write", external_override)
    with pytest.raises(DocumentConflict):
        service.save_override(workspace["id"], note["id"], note["revision"], HEADER + "My edit")
    assert service.read(workspace["id"], "Note.md")["text"] == HEADER + "Other editor"


def test_legacy_and_paged_indexes_use_effective_text_and_invalidate_generations(setup):
    _, service, workspace, _ = setup
    old_pages = pages(service, workspace)
    generation = old_pages[-1]["generation"]
    assert pages(service, workspace, known_generation=generation)[-1]["not_modified"]
    note = saved(setup)
    assert service.index(workspace["id"])["documents"][0]["text"] == note["text"]
    with pytest.raises(DocumentIndexChanged):
        service.index_page(workspace["id"], known_generation=generation)
    current_pages = pages(service, workspace)
    rows = [row for page in current_pages for row in page["documents"]]
    assert rows[0]["text"] == note["text"] and rows[0]["override_saved"] and not rows[0]["editable"]
    assert pages(service, workspace, known_generation=current_pages[-1]["generation"])[-1]["not_modified"]


@pytest.mark.parametrize("body", ["---\ntitle: [unclosed\n---\nBody", "---\nlist: &a [*a]\n---\nBody", "---\nnever closes", "Bare\rline\n"])
def test_unsafe_generated_formats_are_readable_without_override_capability(setup, body):
    root, service, workspace, _ = setup
    (root / "Note.md").write_bytes(body.encode())
    note = service.read(workspace["id"], "Note.md")
    assert not note["override_available"] and not note["editable"]


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "invalid-json", "traversal-record"])
def test_unsafe_override_entries_cannot_be_read_or_used_as_index_snapshots(setup, tmp_path, kind):
    _, service, workspace, store = setup
    saved(setup)
    path = next(store.rglob("*.json"))
    if kind in ["symlink", "hardlink"]:
        target = tmp_path / "target.json"
        target.write_bytes(path.read_bytes())
        path.unlink()
        if kind == "symlink":
            path.symlink_to(target)
        else:
            os.link(target, path)
    elif kind == "invalid-json":
        path.write_text("{")
    else:
        record = json.loads(path.read_text())
        record["path"] = "../escape.md"
        path.write_text(json.dumps(record))
    with pytest.raises(DocumentError):
        service.read(workspace["id"], "Note.md")


def test_store_location_symlink_overlap_permissions_and_write_failure(setup, tmp_path, monkeypatch):
    root, service, workspace, store = setup
    inside = DocumentService(journal_root=tmp_path / "journal3", override_root=root / "overrides")
    try:
        opened = inside.open(root)
        assert not inside.read(opened["id"], "Note.md")["override_available"]
    finally:
        inside.close()
    target = tmp_path / "elsewhere"
    target.mkdir()
    store.symlink_to(target, target_is_directory=True)
    with pytest.raises(DocumentError):
        service.read(workspace["id"], "Note.md")
    assert not list(target.iterdir())
    store.unlink()
    store.mkdir(mode=0o500)
    assert not service.read(workspace["id"], "Note.md")["override_available"]
    store.chmod(0o700)
    note = service.read(workspace["id"], "Note.md")
    original = (root / "Note.md").read_bytes()
    monkeypatch.setattr(os, "write", lambda *args: (_ for _ in ()).throw(OSError("Synthetic write failure")))
    with pytest.raises(DocumentError):
        service.save_override(workspace["id"], note["id"], note["revision"], HEADER + "My edit")
    assert (root / "Note.md").read_bytes() == original
    assert not list(store.rglob("*.json")) and not list(store.rglob("*.tmp"))


@pytest.mark.parametrize("target,mode", [("directory", 0o755), ("directory", 0o770), ("record", 0o644), ("record", 0o666)])
def test_existing_override_storage_must_remain_private(setup, target, mode):
    _, service, workspace, store = setup
    saved(setup)
    record = next(store.rglob("*.json"))
    path = record.parent if target == "directory" else record
    path.chmod(mode)
    with pytest.raises(DocumentError):
        service.read(workspace["id"], "Note.md")
    with pytest.raises(DocumentError):
        service.index_page(workspace["id"])


def test_override_storage_requires_current_user_ownership(setup, monkeypatch):
    _, service, workspace, _ = setup
    saved(setup)
    user = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: user + 1)
    with pytest.raises(DocumentError, match="owned by the current user"):
        service.read(workspace["id"], "Note.md")


def test_override_pages_bound_effective_bytes_and_keep_canonical_typed_ids(setup, monkeypatch):
    root, service, workspace, _ = setup
    (root / "Other.md").write_text(HEADER.replace("note-one", "note-two") + "Other body")
    registry = root / "02_source_memory/indexes/typed_links.yml"
    registry.write_text(json.dumps({"registry_schema_version": documents.RELATIONSHIP_REGISTRY_SCHEMA_VERSION,
                                   "links": [{"relation_id": "accepted", "source_note_id": "note-one", "target_note_id": "note-two", "relation_type": "supports", "decision_status": "accepted", "active": True}]}))
    note = saved(setup, "\n" + "a" * 500 + "\n[[Other]]\n")
    monkeypatch.setattr(documents, "INDEX_PAGE_BYTES", 600)
    result = pages(service, workspace)
    assert not result[-1]["truncated"], result[-1]["warnings"]
    assert all(sum(len(row["text"].encode()) for row in page["documents"]) <= 600 for page in result)
    assert [row for page in result for row in page["relations"]] == [{"id": "accepted", "source": "Note.md", "target": "Other.md", "type": "supports"}]
    assert service.index(workspace["id"])["relations"] == [{"id": "accepted", "source": "Note.md", "target": "Other.md", "type": "supports"}]


def test_override_save_invalidates_an_inflight_page_and_cached_cursor_replay(setup, monkeypatch):
    _, service, workspace, _ = setup
    monkeypatch.setattr(documents, "INDEX_PAGE_OPERATIONS", 1)
    first = service.index_page(workspace["id"])
    assert not first["complete"]
    cursor = first["next_cursor"]
    second = service.index_page(workspace["id"], cursor=cursor)
    saved(setup)
    with pytest.raises(DocumentIndexChanged):
        service.index_page(workspace["id"], cursor=cursor)


def test_disabled_override_store_preserves_original_payload_contract(setup, tmp_path):
    root, _, _, _ = setup
    plain = DocumentService(journal_root=tmp_path / "journal-disabled")
    try:
        workspace = plain.open(root)
        document = plain.read(workspace["id"], "Note.md")
        assert not any(key.startswith("override_") or key in {"source_revision", "source_changed"} for key in document)
        rows = plain.index(workspace["id"])["documents"]
        assert all(set(row) == {"path", "text", "revision", "editable"} for row in rows)
    finally:
        plain.close()


def test_bom_crlf_properties_are_preserved_while_override_body_uses_editor_lf(setup):
    root, service, workspace, _ = setup
    original = b"\xef\xbb\xbf" + (HEADER + "\nCanonical body.\n").replace("\n", "\r\n").encode()
    (root / "Note.md").write_bytes(original)
    note = service.read(workspace["id"], "Note.md")
    result = service.save_override(workspace["id"], note["id"], note["revision"], HEADER + "My body\n")
    assert result["text"] == HEADER + "My body\n"
    assert (root / "Note.md").read_bytes() == original
