import hashlib
import json
from pathlib import Path

import pytest

from auto_zettelkasten import documents
from auto_zettelkasten.documents import DocumentConflict, DocumentError, DocumentService
from auto_zettelkasten.live_notes import LiveNotes

pytestmark = pytest.mark.skipif(not documents._SAFE_WRITES, reason="Shared Markdown requires directory handles")
HEADER = "---\nnote_id: note-one\nsource_id: source-one\nzotero_item_key: PARENT01\ntitle: Original\ntags: [source]\n# preserve this comment\n---\n"
BINDING = {"server_id": "server-one", "library_type": "user", "library_id": "0", "item_key": "PARENT01", "attachment_key": "PDFKEY01"}


class Registrar:
    def __init__(self):
        self.rows = {}; self.writes = 0; self.fail = None

    def find(self, binding, path):
        return self.rows.get(str(path))

    def register(self, binding, path, *, key, write_token):
        self.writes += 1
        row = {"key": key, "path": str(path), "parentItem": binding["item_key"]}
        self.rows[str(path)] = row
        if self.fail == "lost_ack":
            self.fail = None
            raise TimeoutError("response lost")
        return row

    def verify(self, binding, path, attachment):
        return self.fail != "verify" and self.rows.get(str(path)) == attachment


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "vault"; root.mkdir()
    (root / "auto-zettelkasten.yml").write_text("version: 1\n")
    (root / "Note.md").write_text(HEADER + "Original body.\n")
    service = DocumentService(journal_root=tmp_path / "operations", override_root=tmp_path / "overrides", live_root=tmp_path / "live")
    ws = service.open(root)
    yield root, service, ws, Registrar()
    service.close()


def note(setup):
    _, service, ws, _ = setup
    return service.read(ws["id"], "Note.md")


def migrate(setup, **kwargs):
    _, service, ws, registrar = setup
    current = note(setup)
    return service.migrate_live_note(ws["id"], current["id"], current["revision"], BINDING, registrar, **kwargs)


def test_migration_preserves_body_title_tags_and_stable_path(setup):
    root, service, ws, registrar = setup
    original = (root / "Note.md").read_bytes()
    current = note(setup)
    current = service.save_override(ws["id"], current["id"], current["revision"], HEADER + "My edited body.\n")
    service.save_personal_metadata(ws["id"], current["id"], current["revision"], {"display_title": "Personal title", "personal_properties": {"tags": ["mine"], "aliases": ["Alias"], "rating": 4}})
    saved = migrate(setup)
    assert saved["live_note"] and saved["editable"] and not saved["override_available"]
    assert saved["path"] == "Note.md" and saved["display_title"] == "Personal title"
    assert saved["effective_tags"] == ["source", "mine"]
    assert saved["text"].endswith("My edited body.\n")
    assert "# preserve this comment" in saved["text"]
    assert registrar.writes == 1 and len(registrar.rows) == 1
    record = service._live_notes.read(root, service._root_identity, "Note.md")
    import base64
    assert base64.b64decode(record["baseline_b64"]) == original
    assert list(service._overrides.root.rglob("*.archived-*"))
    assert not list(service._overrides.root.rglob("*.json"))
    assert len(service.recall_corpus(ws["id"])["documents"]) == 1


def test_metadata_only_migration_preserves_semantic_map_identity_honestly(setup):
    root, service, ws, _ = setup
    current = note(setup); baseline = current["revision"]
    service.save_personal_metadata(ws["id"], current["id"], current["revision"], {"display_title": "Mine"})
    saved = migrate(setup)
    assert saved["live_equivalent_revision"] == baseline
    assert saved["revision"] != baseline
    assert not saved["body_override_saved"]
    projected = service._semantic_document("Note.md", saved["text"], saved, saved["revision"])
    assert projected["projection_hash"] == baseline and projected["effective_unchanged"]
    updated = service.save(ws["id"], saved["id"], saved["revision"], saved["text"].replace("Original body", "New finding"))
    assert updated["live_equivalent_revision"] is None and updated["body_override_saved"]
    projected = service._semantic_document("Note.md", updated["text"], updated, updated["revision"])
    assert not projected["effective_unchanged"]


def test_active_file_is_single_writer_and_external_edits_are_visible(setup):
    root, service, ws, _ = setup
    saved = migrate(setup)
    edited = service.save_override(ws["id"], saved["id"], saved["revision"], saved["text"] + "App edit\n")
    assert (root / "Note.md").read_text() == edited["text"]
    (root / "Note.md").write_text(edited["text"] + "Obsidian edit\n")
    with pytest.raises(DocumentConflict):
        service.save(ws["id"], edited["id"], edited["revision"], edited["text"] + "Stale draft\n")
    external = note(setup)
    assert external["text"].endswith("Obsidian edit\n")
    with pytest.raises(DocumentError, match="identity"):
        service.save(ws["id"], external["id"], external["revision"], external["text"].replace("source-one", "source-two"))
    (root / "Note.md").write_text(external["text"].replace("source-one", "source-two"))
    conflict = note(setup)
    assert not conflict["editable"] and not conflict["live_identity_valid"]
    assert "source-two" in conflict["text"]


def test_prepared_resume_and_lost_ack_never_duplicate_attachment(setup):
    root, service, ws, registrar = setup
    current = note(setup)
    prepared = service.prepare_live_note(ws["id"], current["id"], current["revision"], BINDING)
    assert prepared["phase"] == "prepared"
    assert not note(setup)["editable"]
    registrar.fail = "lost_ack"
    with pytest.raises(DocumentError, match="outcome is unknown"):
        migrate(setup)
    assert note(setup)["live_note_phase"] == "written"
    saved = migrate(setup)
    assert saved["editable"] and registrar.writes == 1
    assert migrate(setup)["editable"] and registrar.writes == 1


def test_failed_attachment_verification_preserves_overlay(setup):
    root, service, ws, registrar = setup
    current = note(setup)
    service.save_override(ws["id"], current["id"], current["revision"], HEADER + "Important edit\n")
    registrar.fail = "verify"
    with pytest.raises(DocumentError, match="read back"):
        migrate(setup)
    assert list(service._overrides.root.rglob("*.json"))
    assert note(setup)["text"].endswith("Important edit\n")
    registrar.fail = None
    assert migrate(setup)["editable"]


def test_global_binding_rejects_second_workspace_but_library_scope_is_distinct(setup, tmp_path):
    root, service, ws, registrar = setup
    migrate(setup)
    other = tmp_path / "derivative"; other.mkdir()
    (other / "Note.md").write_text(HEADER + "Derivative\n")
    second = DocumentService(override_root=tmp_path / "overrides", live_root=tmp_path / "live")
    second_ws = second.open(other); current = second.read(second_ws["id"], "Note.md")
    assert not current.get("live_note")
    with pytest.raises(DocumentError, match="another workspace"):
        second.preview_live_note(second_ws["id"], current["id"], current["revision"], BINDING)
    preview = second.preview_live_note(second_ws["id"], current["id"], current["revision"], {**BINDING, "server_id": "other-server"})
    assert not preview["already_active"]
    second.close()


def test_prepared_external_change_is_not_overwritten(setup):
    root, service, ws, registrar = setup
    current = note(setup)
    service.prepare_live_note(ws["id"], current["id"], current["revision"], BINDING)
    (root / "Note.md").write_text(HEADER + "External edit after preparation\n")
    with pytest.raises(DocumentError, match="changed after migration"):
        migrate(setup)
    assert (root / "Note.md").read_text().endswith("External edit after preparation\n")
    assert registrar.writes == 0


def test_personal_metadata_saves_use_same_revision_safe_file(setup):
    root, service, ws, _ = setup
    saved = migrate(setup)
    updated = service.save_personal_metadata(ws["id"], saved["id"], saved["revision"], {"display_title": "Edited title", "personal_properties": {"tags": ["source", "personal"], "rating": 5}})
    assert updated["display_title"] == "Edited title" and updated["effective_tags"] == ["source", "personal"]
    assert "rating" in (root / "Note.md").read_text()
    assert updated["text"].endswith("Original body.\n")
    with pytest.raises(DocumentConflict):
        service.save_personal_metadata(ws["id"], saved["id"], saved["revision"], {"display_title": "Stale"})
    reset = service.save_personal_metadata(ws["id"], updated["id"], updated["revision"], {"display_title": None})
    assert reset["display_title"] == "Original"


def test_invalid_duplicate_headers_or_binding_never_write(setup):
    root, service, ws, registrar = setup
    (root / "Note.md").write_text(HEADER.replace("note_id: note-one", "note_id: note-one\nnote_id: duplicate") + "Body")
    with pytest.raises(DocumentError, match="duplicate"):
        migrate(setup)
    assert not registrar.rows
    assert not list(service._live_notes.root.rglob("*.json"))


def test_restore_pending_migration_keeps_current_recovery_bytes(setup):
    root, service, ws, _ = setup
    current = note(setup)
    service.migrate_live_note(ws["id"], current["id"], current["revision"], BINDING)
    current = note(setup)
    restored = service.restore_live_note(ws["id"], current["id"], current["revision"], Registrar())
    assert not restored.get("live_note") and restored["text"] == HEADER + "Original body.\n"


def test_legacy_v1_body_is_materialized(setup):
    root, service, ws, _ = setup
    current = note(setup)
    service.save_override(ws["id"], current["id"], current["revision"], HEADER + "Legacy edit\n")
    filename = next(service._overrides.root.rglob("*.json"))
    record = json.loads(filename.read_text()); record["version"] = 1
    filename.write_text(json.dumps(record))
    assert migrate(setup)["text"].endswith("Legacy edit\n")


def test_generator_stages_new_baseline_without_replacing_active_file_or_sidecar(tmp_path, monkeypatch):
    from auto_zettelkasten.notes import write_limited_note, update_note_frontmatter, update_note_graph, update_note_literature
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / "workspace"; root.mkdir()
    fields = {"note_id": "note-one", "source_id": "source-one", "zotero_item_key": "PARENT01",
              "note_status": "metadata_only_source_note", "source_scope": "metadata_only",
              "source_file": "zotero://select/library/items/PARENT01", "inspected_content_hash": "b" * 64,
              "content_route": "metadata_only", "original_zotero_tags": [], "normalized_tags": [],
              "related_notes": [], "source_coverage": {"gate": "failed"}, "title": "Synthetic source"}
    path, validation = write_limited_note(root, fields, {"metadata": {"title": "Original metadata"}})
    assert validation.passed
    config = tmp_path / ".config" / "auto-zettelkasten"
    service = DocumentService(override_root=config / "workspace-overrides")
    ws = service.open(root); doc = service.read(ws["id"], path.relative_to(root).as_posix())
    shared = service.migrate_live_note(ws["id"], doc["id"], doc["revision"], BINDING, Registrar())
    shared = service.save(ws["id"], shared["id"], shared["revision"], shared["text"] + "\nMy external notes.\n")
    before = path.read_bytes()
    sidecar = root / "11_state" / "note_metadata" / "note-one.yml"
    original_sidecar = sidecar.read_bytes()
    target, result = write_limited_note(root, {**fields, "title": "Changed title"}, {"metadata": {"title": "New upstream result"}})
    assert target == path and not result.passed and "active_note_baseline_review_required" in result.errors
    assert path.read_bytes() == before and sidecar.read_bytes() == original_sidecar
    journal = service._live_notes.read(service._root, service._root_identity, doc["path"])
    assert journal["baseline_review_required"]
    assert list(service._live_notes.root.rglob("baseline-*.json"))
    update_note_frontmatter(path, {"title": "Another generated title"})
    assert not update_note_literature(path, [{"author": "Synthetic", "engagement": "Updated context"}])
    assert not update_note_graph(path, {}, [], [])
    assert path.read_bytes() == before and sidecar.read_bytes() == original_sidecar
    service.close()


def test_annotation_receipt_preserves_summary_equivalence_only_for_verified_block(setup):
    from auto_zettelkasten.zotero_annotations import project_annotations
    root, service, ws, _ = setup
    saved = migrate(setup)
    result = project_annotations(saved["text"], [])
    saved = service.save(ws["id"], saved["id"], saved["revision"], result["text"])
    assert saved["live_equivalent_revision"] is None
    confirmed = service.confirm_annotation_projection(ws["id"], saved["id"], saved["revision"], result["block_hash"])
    assert confirmed["live_equivalent_revision"] and confirmed["semantic_baseline_text"] == HEADER + "Original body.\n"
    changed = service.save(ws["id"], confirmed["id"], confirmed["revision"], confirmed["text"].replace("No confirmed annotations.", "My unverified interpretation."))
    assert changed["live_equivalent_revision"] is None
    with pytest.raises(DocumentError, match="differs"):
        service.confirm_annotation_projection(ws["id"], changed["id"], changed["revision"], result["block_hash"])


@pytest.mark.parametrize("phase", ["prepared", "written", "attachment_registered", "verified", "archived"])
def test_resume_at_every_journal_boundary_is_idempotent(setup, monkeypatch, phase):
    import auto_zettelkasten.live_notes as module
    root, service, ws, registrar = setup
    current = note(setup)
    service.save_override(ws["id"], current["id"], current["revision"], HEADER + "Recovery edit\n")
    original_write = module._write
    failed = False
    def interrupt(directory, name, value):
        nonlocal failed
        if name.startswith("binding-") and value.get("phase") == phase and not failed:
            failed = True
            raise RuntimeError("simulated interruption")
        return original_write(directory, name, value)
    monkeypatch.setattr(module, "_write", interrupt)
    with pytest.raises(RuntimeError):
        migrate(setup)
    monkeypatch.setattr(module, "_write", original_write)
    final = migrate(setup)
    assert final["editable"] and final["text"].endswith("Recovery edit\n")
    assert registrar.writes == 1


def test_resume_after_binding_saved_before_path_pointer(setup, monkeypatch):
    import auto_zettelkasten.live_notes as module
    root, service, ws, registrar = setup
    original_write = module._write
    def interrupt(directory, name, value):
        if name.startswith("path-"):
            raise RuntimeError("interrupted path receipt")
        return original_write(directory, name, value)
    monkeypatch.setattr(module, "_write", interrupt)
    with pytest.raises(RuntimeError):
        migrate(setup)
    monkeypatch.setattr(module, "_write", original_write)
    assert migrate(setup)["editable"] and registrar.writes == 1


def test_restore_completed_migration_requires_detachment_and_retains_edits(setup):
    root, service, ws, registrar = setup
    initial = note(setup)
    service.save_override(ws["id"], initial["id"], initial["revision"], HEADER + "Previous override\n")
    saved = migrate(setup)
    saved = service.save(ws["id"], saved["id"], saved["revision"], saved["text"] + "New active edits\n")
    with pytest.raises(DocumentError, match="still exists"):
        service.restore_live_note(ws["id"], saved["id"], saved["revision"], registrar)
    registrar.rows.clear()  # synthetic user detached the linked attachment in Zotero
    restored = service.restore_live_note(ws["id"], saved["id"], saved["revision"], registrar)
    assert restored["text"].endswith("Previous override\n")
    records = [json.loads(p.read_text()) for p in service._live_notes.root.rglob("binding-*.json")]
    import base64
    assert base64.b64decode(records[0]["restored_active_b64"]).endswith(b"New active edits\n")


def test_migration_remains_private_and_refuses_linked_record_paths(setup, tmp_path):
    import os
    root, service, ws, registrar = setup
    migrate(setup)
    assert not list(root.rglob("*.json"))
    journal = next(service._live_notes.root.rglob("binding-*.json"))
    assert journal.stat().st_mode & 0o777 == 0o600
    moved = tmp_path / "outside.json"; journal.rename(moved); journal.symlink_to(moved)
    with pytest.raises(DocumentError):
        note(setup)


def test_read_only_active_file_cannot_be_saved(setup):
    root, service, ws, _ = setup
    current = migrate(setup)
    (root / "Note.md").chmod(0o400)
    current = note(setup)
    assert not current["editable"]
    with pytest.raises(DocumentError, match="read-only"):
        service.save(ws["id"], current["id"], current["revision"], current["text"] + "No")


def test_remigration_keeps_earlier_active_recovery_text(setup):
    import base64
    root, service, ws, registrar = setup
    first = migrate(setup)
    first = service.save(ws["id"], first["id"], first["revision"], first["text"] + "Important first active edit\n")
    registrar.rows.clear()
    service.restore_live_note(ws["id"], first["id"], first["revision"], registrar)
    migrate(setup)
    history = [json.loads(path.read_text()) for path in service._live_notes.root.rglob("history-*.json")]
    assert len(history) == 1
    assert base64.b64decode(history[0]["restored_active_b64"]).endswith(b"Important first active edit\n")


def test_restore_restart_does_not_replace_recovery_edits_with_baseline(setup, monkeypatch):
    import base64
    import auto_zettelkasten.live_notes as module
    root, service, ws, registrar = setup
    original = note(setup)
    service.save_override(ws["id"], original["id"], original["revision"], HEADER + "Original overlay\n")
    current = migrate(setup)
    current = service.save(ws["id"], current["id"], current["revision"], current["text"] + "Precious active edits\n")
    registrar.rows.clear()
    real_write = module._write
    def interrupt(directory, name, value):
        if name.startswith("binding-") and value.get("phase") == "restored":
            raise RuntimeError("crash after file and overlay restoration")
        return real_write(directory, name, value)
    monkeypatch.setattr(module, "_write", interrupt)
    with pytest.raises(RuntimeError):
        service.restore_live_note(ws["id"], current["id"], current["revision"], registrar)
    monkeypatch.setattr(module, "_write", real_write)
    current = note(setup)
    assert current["live_note_phase"] == "restoring"
    restored = service.restore_live_note(ws["id"], current["id"], current["revision"], registrar)
    assert restored["text"].endswith("Original overlay\n")
    record = json.loads(next(service._live_notes.root.rglob("binding-*.json")).read_text())
    assert base64.b64decode(record["restored_active_b64"]).endswith(b"Precious active edits\n")


def test_default_global_binding_store_is_independent_of_override_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    first = DocumentService(override_root=tmp_path / "private-first" / "overrides")
    second = DocumentService(override_root=tmp_path / "private-other" / "overrides")
    assert first._live_notes.root == second._live_notes.root == tmp_path / ".config/auto-zettelkasten/workspace-live-notes"
    first.close(); second.close()


def test_verified_sidecar_navigation_is_visible_without_rewriting_canonical_notes(setup):
    import yaml
    from auto_zettelkasten.notes import NOTE_METADATA_SCHEMA_VERSION
    root, service, ws, _ = setup
    # Public writer output omits the hidden identity and source links.
    (root / "Note.md").write_text("---\nnote_id: note-one\ntitle: Original\n---\nSource body.\n")
    original = (root / "Note.md").read_bytes()
    sidecars = root / "11_state" / "note_metadata"; sidecars.mkdir(parents=True)
    payload = {"metadata_schema_version": NOTE_METADATA_SCHEMA_VERSION, "note_path": "Note.md", "frontmatter": {
        "note_id": "note-one", "source_id": "source-one", "zotero_item_key": "PARENT01",
        "zotero_item_uri": "zotero://select/library/items/PARENT01", "attachment_key": "PDFKEY01",
        "source_pdf_uri": "zotero://open-pdf/library/items/PDFKEY01?page=1"}}
    (sidecars / "note-one.yml").write_text(yaml.safe_dump(payload))
    current = note(setup)
    assert current["source_navigation"]["verified"]
    assert current["source_navigation"]["attachment_key"] == "PDFKEY01"
    corpus = service.recall_corpus(ws["id"])
    assert corpus["documents"][0]["source_navigation"]["source_id"] == "source-one"
    assert (root / "Note.md").read_bytes() == original
    saved = migrate(setup)
    projected = documents._frontmatter(saved["text"])
    assert projected["source_pdf_uri"] == payload["frontmatter"]["source_pdf_uri"] and projected["source_id"] == "source-one"
    assert "source_pdf_uri" in saved["live_protected_fields"]
    assert saved["live_equivalent_revision"] == hashlib.sha256(original).hexdigest()
    assert saved["semantic_baseline_text"].encode() == original


def test_recall_navigation_cache_invalidates_when_sidecar_changes(setup):
    import yaml
    from auto_zettelkasten.notes import NOTE_METADATA_SCHEMA_VERSION
    root, service, ws, _ = setup
    (root / "Note.md").write_text("---\nnote_id: note-one\n---\nBody\n")
    directory = root / "11_state" / "note_metadata"; directory.mkdir(parents=True)
    payload = {"metadata_schema_version": NOTE_METADATA_SCHEMA_VERSION, "note_path": "Note.md", "frontmatter": {
        "note_id": "note-one", "source_id": "source-one", "attachment_key": "FIRSTPDF"}}
    path = directory / "note-one.yml"; path.write_text(yaml.safe_dump(payload))
    first = service.recall_corpus(ws["id"])
    assert first["documents"][0]["source_navigation"]["attachment_key"] == "FIRSTPDF"
    payload["frontmatter"]["attachment_key"] = "NEXTPDFX"; path.write_text(yaml.safe_dump(payload))
    second = service.recall_corpus(ws["id"])
    assert second["documents"][0]["source_navigation"]["attachment_key"] == "NEXTPDFX"
    payload["note_path"] = "Wrong.md"; path.write_text(yaml.safe_dump(payload))
    assert "source_navigation" not in service.recall_corpus(ws["id"])["documents"][0]


def test_large_alias_bearing_sidecar_projects_only_direct_navigation(setup):
    from auto_zettelkasten.notes import NOTE_METADATA_SCHEMA_VERSION
    root, service, ws, _ = setup
    (root / "Note.md").write_text("---\nnote_id: note-one\ntitle: Source\n---\nOriginal body\n")
    folder = root / "11_state" / "note_metadata"; folder.mkdir(parents=True)
    raw = ('metadata_schema_version: "' + NOTE_METADATA_SCHEMA_VERSION + '"\nnote_path: Note.md\n'
           'frontmatter:\n  note_id: note-one\n  source_id: source-one\n  zotero_item_key: PARENT01\n'
           '  attachment_key: PDFKEY01\n  zotero_item_uri: zotero://select/library/items/PARENT01\n'
           '  source_pdf_uri: zotero://open-pdf/library/items/PDFKEY01?page=1\n'
           '  coverage: &coverage\n    page_indexes: [1, 2, 3]\n    description: "' + "synthetic coverage " * 6000 + '"\n'
           '  recovered_coverage: *coverage\n  attempts: [{coverage: *coverage}]\n')
    assert 65536 < len(raw.encode()) < 1024 * 1024
    (folder / "note-one.yml").write_text(raw)
    projected = documents._origin_identity(raw.encode(), include_source_navigation=True)
    assert set(projected["frontmatter"]) == {"note_id", "source_id", "zotero_item_key", "attachment_key", "zotero_item_uri", "source_pdf_uri"}
    assert documents._origin_identity(raw.encode()) == {}  # existing compact origin contract unchanged
    current = note(setup)
    assert current["source_navigation"]["attachment_key"] == "PDFKEY01"
    assert "coverage" not in current["source_navigation"]
    assert service.recall_corpus(ws["id"])["documents"][0]["source_navigation"]["verified"]
    saved = migrate(setup)
    assert saved["editable"] and saved["live_equivalent_revision"]


@pytest.mark.parametrize("fragment", [
    "  zotero_item_key: PARENT01\n  zotero_item_key: OTHERKEY\n",
    "  attachment_key: &first PDFKEY01\n  source_pdf_uri: *first\n",
    "  zotero_item_uri: [zotero://select/library/items/PARENT01]\n",
    "  source_id: duplicate\n",
    "  <<: {source_pdf_uri: zotero://open-pdf/library/items/PDFKEY01}\n",
    "  source_pdf_uri: " + "x" * 4097 + "\n",
])
def test_navigation_scalar_projection_rejects_ambiguous_or_oversized_claims(fragment):
    raw = ('metadata_schema_version: "2"\nnote_path: Note.md\nfrontmatter:\n'
           '  note_id: note-one\n  source_id: source-one\n' + fragment).encode()
    assert documents._origin_identity(raw, include_source_navigation=True) == {}


def test_navigation_projection_keeps_byte_event_and_depth_limits():
    header = 'metadata_schema_version: "2"\nnote_path: Note.md\nfrontmatter:\n  note_id: note-one\n  source_id: source-one\n'
    assert documents._origin_identity((header + 'padding: "' + 'x' * 1024 * 1024 + '"').encode(), include_source_navigation=True) == {}
    assert documents._origin_identity((header + 'items: [' + ','.join('x' for _ in range(33000)) + ']').encode(), include_source_navigation=True) == {}
    assert documents._origin_identity((header + 'nested: ' + '[' * 34 + 'x' + ']' * 34).encode(), include_source_navigation=True) == {}
