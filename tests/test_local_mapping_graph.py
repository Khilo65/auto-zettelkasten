"""Synthetic read-only projection of a cached semantic map into workspace graphs."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess

import pytest
import yaml

from auto_zettelkasten import documents
from auto_zettelkasten.documents import DocumentIndexChanged, DocumentService
from auto_zettelkasten.notes import NOTE_METADATA_SCHEMA_VERSION, public_note_frontmatter
from auto_zettelkasten.relationships import RELATIONSHIP_REGISTRY_SCHEMA_VERSION


def _sha(value):
    return hashlib.sha256(value).hexdigest()


@pytest.fixture
def mapped(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "auto-zettelkasten.yml").write_text("engine_version: synthetic\n")
    notes = root / "02_source_memory/notes"
    profiles = root / "02_source_memory/profiles"
    indexes = root / "02_source_memory/indexes"
    for directory in (notes, profiles, indexes):
        directory.mkdir(parents=True)
    sources = []
    for name in ("a", "b"):
        note = notes / f"{name}.md"
        text = f"---\nsource_id: source-{name}\nnote_id: note-{name}\ntype: source\n---\n# Source {name}\nSynthetic authored material.\n"
        note.write_bytes(text.encode() if name == "a" else b"\xef\xbb\xbf" + text.replace("\n", "\r\n").encode())
        profile = profiles / f"note-{name}.yml"
        profile.write_text(f"profile:\n  source_id: source-{name}\n  note_id: note-{name}\n")
        sources.append({"source_id": f"source-{name}", "note_id": f"note-{name}",
                        "note_path": str(note.relative_to(root)), "note_hash": "preserved-semantic-hash",
                        "profile_hash": "preserved-profile-content-hash", "projection_hash": _sha(note.read_bytes()),
                        "profile_file_sha256": _sha(profile.read_bytes()), "chunk_ids": [f"chunk-{name}"],
                        "coverage": {"status": "included"}})
    registry = {"registry_schema_version": RELATIONSHIP_REGISTRY_SCHEMA_VERSION, "links": [
        {"relation_id": "accepted", "relation_type": "supports", "decision_status": "accepted",
         "source_note_id": "note-a", "target_note_id": "note-b"},
        {"relation_id": "citation", "relation_type": "cites", "decision_status": "accepted",
         "source_note_id": "note-a", "target_note_id": "note-b"}]}
    (indexes / "typed_links.yml").write_text(yaml.safe_dump(registry))
    payload = {"schema_version": "local-semantic-map-v1", "identity": {"digest": "1" * 64, "private_path": "/never/expose"},
               "sources": sources, "communities": [{"community_id": "semantic-pair", "algorithm": "leiden", "source_ids": ["source-a", "source-b"]}],
               "neighbors": [{"source_id": "source-a", "target_source_id": "source-b", "score": 0.75,
                              "provenance": {"source_chunk_ids": ["chunk-a"], "target_chunk_ids": ["chunk-b"], "private_text": "never expose"}}],
               "stats": {"private_path": "/never/expose"}}
    result = root / ".auto-zettelkasten/mapping/result.json"
    result.parent.mkdir(parents=True)
    result.write_text(json.dumps(payload))
    service = DocumentService(journal_root=tmp_path / "journal", override_root=tmp_path / "overrides")
    opened = service.open(root)
    yield root, service, opened["id"], payload, result
    service.close()


def _pages(service, workspace_id, **kwargs):
    result = service.index_page(workspace_id, **kwargs)
    rows, relations = list(result["documents"]), list(result["relations"])
    while not result["complete"]:
        result = service.index_page(workspace_id, result["next_cursor"])
        rows.extend(result["documents"])
        relations.extend(result["relations"])
    return {**result, "documents": rows, "relations": relations}


def test_legacy_and_paged_project_only_owned_suggestions_without_any_writes(mapped, monkeypatch):
    root, service, workspace_id, payload, _ = mapped
    before = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    def forbidden(*args, **kwargs):
        pytest.fail("Projection must not load models, launch jobs or use the network")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    legacy = service.index(workspace_id)
    paged = _pages(service, workspace_id)
    for snapshot in (legacy, paged):
        assert len(snapshot["documents"]) == 2
        assert len(snapshot["relations"]) == 3
        relation = next(row for row in snapshot["relations"] if row.get("derived"))
        assert relation["type"] == "semantic_similarity"
        assert relation["decision_status"] == "suggested"
        assert relation["accepted"] is relation["evidence_backed"] is False
        assert relation["score"] == 0.75
        assert relation["provenance"]["source_chunk_ids"] == ["chunk-a"]
        assert "private_text" not in relation["provenance"]
        assert snapshot["semantic_map"]["status"] == "current"
        assert snapshot["semantic_map"]["communities"] == [{"community_id": "semantic-pair", "algorithm": "leiden", "paths": [row["note_path"] for row in payload["sources"]]}]
        assert "/never/expose" not in json.dumps(snapshot)
    assert {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("changed", ["note", "profile", "source_identity", "ambiguous_owner", "override"])
def test_changed_inputs_or_ownership_remove_stale_derived_edges(mapped, changed):
    root, service, workspace_id, _, _ = mapped
    note = root / "02_source_memory/notes/a.md"
    if changed == "note":
        note.write_text(note.read_text() + "Changed source text.\n")
    elif changed == "profile":
        profile = root / "02_source_memory/profiles/note-a.yml"
        profile.write_text(profile.read_text() + "changed: true\n")
    elif changed == "source_identity":
        note.write_text(note.read_text().replace("source-a", "another-source"))
    elif changed == "ambiguous_owner":
        (note.parent / "other.md").write_text(note.read_text().replace("note-a", "other-note"))
    else:
        if not documents._SAFE_WRITES:
            pytest.skip("Safe override writes are unavailable")
        opened = service.read(workspace_id, "02_source_memory/notes/a.md")
        service.save_override(workspace_id, opened["id"], opened["revision"], opened["text"] + "Personal correction.\n")
    for snapshot in (service.index(workspace_id), _pages(service, workspace_id)):
        assert not [row for row in snapshot["relations"] if row.get("derived")]
        assert len([row for row in snapshot["relations"] if row["type"] in {"supports", "cites"}]) == 2
        assert snapshot["semantic_map"]["status"] == "partial"
        assert snapshot["semantic_map"]["communities"][0]["paths"] == ["02_source_memory/notes/b.md"]
        assert any("stale" in warning for warning in snapshot["warnings"])


@pytest.mark.parametrize("change", ["result", "profile", "metadata"])
def test_result_and_profile_changes_invalidate_completed_index_generation(mapped, change):
    root, service, workspace_id, payload, result = mapped
    first = _pages(service, workspace_id)
    assert _pages(service, workspace_id, known_generation=first["generation"])["not_modified"]
    if change == "result":
        payload["neighbors"][0]["score"] = 0.25
        result.write_text(json.dumps(payload))
    elif change == "profile":
        profile = root / "02_source_memory/profiles/note-a.yml"
        profile.write_text(profile.read_text() + "changed: true\n")
    else:
        metadata = root / "11_state/note_metadata/note-a.yml"
        metadata.parent.mkdir(parents=True)
        metadata.write_text("changed: true\n")
    with pytest.raises(DocumentIndexChanged):
        _pages(service, workspace_id, known_generation=first["generation"])


def test_result_change_during_index_rejects_mixed_snapshot(mapped, monkeypatch):
    _, service, workspace_id, payload, result = mapped
    monkeypatch.setattr(documents, "INDEX_PAGE_DOCUMENTS", 1)
    page = service.index_page(workspace_id)
    assert not page["complete"]
    payload["neighbors"] = []
    result.write_text(json.dumps(payload))
    with pytest.raises(DocumentIndexChanged):
        service.index_page(workspace_id, page["next_cursor"])


@pytest.mark.parametrize("changed", ["result", "profile", "metadata"])
def test_legacy_projection_rechecks_derived_inputs_before_returning(mapped, changed, monkeypatch):
    root, service, workspace_id, payload, result = mapped
    project = service._project_semantic_map
    def changed_after_projection(*args):
        projected = project(*args)
        if changed == "result":
            payload["neighbors"] = []
            result.write_text(json.dumps(payload))
        elif changed == "profile":
            profile = root / "02_source_memory/profiles/note-a.yml"
            profile.write_text(profile.read_text() + "changed: true\n")
        else:
            metadata = root / "11_state/note_metadata/note-a.yml"
            metadata.parent.mkdir(parents=True)
            metadata.write_text("changed: true\n")
        return projected
    monkeypatch.setattr(service, "_project_semantic_map", changed_after_projection)
    with pytest.raises(DocumentIndexChanged):
        service.index(workspace_id)


def test_backend_results_flow_through_both_actual_javascript_graph_indexes(mapped):
    repository = Path(__file__).resolve().parents[1]
    node = shutil.which("node")
    if not node or not (repository / "node_modules/markdown-it").is_dir():
        pytest.skip("Node and installed web dependencies are required")
    _, service, workspace_id, _, _ = mapped
    snapshots = [service.index(workspace_id), _pages(service, workspace_id)]
    script = """
import fs from 'node:fs';
import { buildLinkIndex, buildLibraryIndex, graphData, parseLibraryDocument } from './tools/workspace_links.js';
const output = JSON.parse(fs.readFileSync(0, 'utf8')).flatMap(snapshot => [
  buildLinkIndex(snapshot.documents, snapshot.relations, null, snapshot),
  buildLibraryIndex(snapshot.documents.map(parseLibraryDocument), snapshot.relations, null, snapshot)
].map(index => graphData(index, {showReferences:false,showCitations:false,showRelations:false,showNavigation:false,showDiscovery:true})));
console.log(JSON.stringify(output));
"""
    completed = subprocess.run([node, "--input-type=module", "-e", script], input=json.dumps(snapshots),
                               capture_output=True, text=True, cwd=repository, timeout=30)
    assert completed.returncode == 0, completed.stderr
    for graph in json.loads(completed.stdout):
        assert len(graph["nodes"]) == 2
        assert len(graph["edges"]) == 1
        assert graph["edges"][0]["layer"] == "discovery"
        assert graph["edges"][0]["score"] == 0.75
        assert all(row["semantic_communities"] == [{"community_id": "semantic-pair", "algorithm": "leiden"}] for row in graph["nodes"])


@pytest.mark.parametrize("invalid", [None, "schema", "note_path", "source_id", "explicit_conflict", "missing"])
def test_real_public_note_identity_uses_only_its_bound_protected_metadata(mapped, invalid):
    root, service, workspace_id, payload, result = mapped
    source = payload["sources"][0]
    front = {"note_id": "note-a", "source_id": "source-a", "title": "Readable source", "note_status": "analytical_atomic_note", "source_scope": "full_document"}
    public = public_note_frontmatter(front)
    assert "source_id" not in public, "The fixture must use the actual public source-note contract"
    if invalid == "explicit_conflict":
        public["source_id"] = "another-source"
    note = root / source["note_path"]
    note.write_text("---\n" + yaml.safe_dump(public) + "---\n## Thesis\nSynthetic authored material.\n")
    source["projection_hash"] = _sha(note.read_bytes())
    metadata = {"metadata_schema_version": NOTE_METADATA_SCHEMA_VERSION, "note_path": source["note_path"], "frontmatter": front}
    if invalid == "schema":
        metadata["metadata_schema_version"] = "unknown"
    elif invalid == "note_path":
        metadata["note_path"] = "02_source_memory/notes/other.md"
    elif invalid == "source_id":
        metadata["frontmatter"]["source_id"] = "another-source"
    if invalid != "missing":
        sidecar = root / "11_state/note_metadata/note-a.yml"
        sidecar.parent.mkdir(parents=True)
        sidecar.write_text(yaml.safe_dump(metadata))
        source["metadata_file_sha256"] = _sha(sidecar.read_bytes())
        source["metadata_path"] = str(sidecar.relative_to(root))
    result.write_text(json.dumps(payload))
    for snapshot in (service.index(workspace_id), _pages(service, workspace_id)):
        derived = [row for row in snapshot["relations"] if row.get("derived")]
        assert len(derived) == (0 if invalid else 1)
        assert snapshot["semantic_map"]["status"] == ("partial" if invalid else "current")


def test_protected_dependency_budget_reports_partial_coverage(mapped, monkeypatch):
    root, service, workspace_id, _, _ = mapped
    monkeypatch.setattr(documents, "_SEMANTIC_DEPENDENCY_BYTES", (root / "02_source_memory/profiles/note-a.yml").stat().st_size)
    snapshot = service.index(workspace_id)
    assert not [row for row in snapshot["relations"] if row.get("derived")]
    assert snapshot["semantic_map"]["status"] == "partial"
    assert snapshot["semantic_map"]["coverage"]["included"] == 1
    assert snapshot["semantic_map"]["coverage"]["dependency_budget_reached"] is True
    assert any("byte budget" in warning for warning in snapshot["warnings"])


def test_optional_density_noise_status_survives_projection_without_hdbscan_dependency(mapped, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "hdbscan", None)
    _, service, workspace_id, payload, result = mapped
    payload["communities"] = [
        {"community_id": f"semantic-noise-{name}", "algorithm": "hdbscan", "source_ids": [f"source-{name}"], "noise": True}
        for name in ("a", "b")]
    result.write_text(json.dumps(payload))
    for snapshot in (service.index(workspace_id), _pages(service, workspace_id)):
        groups = snapshot["semantic_map"]["communities"]
        assert len(groups) == 2
        assert all(group["noise"] is True and group["algorithm"] == "hdbscan" and len(group["paths"]) == 1 for group in groups)
        assert len(snapshot["documents"]) == 2, "Density noise must not create a common hub document"


@pytest.mark.parametrize("invalid", ["json", "foreign_chunk", "duplicate_source", "path_escape", "nan", "oversized", "identity"])
def test_malformed_cache_is_explicit_without_breaking_accepted_graph(mapped, invalid, monkeypatch):
    _, service, workspace_id, payload, result = mapped
    if invalid == "json":
        result.write_text("{broken")
    elif invalid == "oversized":
        monkeypatch.setattr(documents, "MAX_REGISTRY_BYTES", 100)
    else:
        if invalid == "foreign_chunk":
            payload["neighbors"][0]["provenance"]["source_chunk_ids"] = ["chunk-b"]
        elif invalid == "duplicate_source":
            payload["sources"].append(payload["sources"][0])
        elif invalid == "path_escape":
            payload["sources"][0]["note_path"] = "../outside.md"
        elif invalid == "identity":
            payload["identity"]["digest"] = int("1" * 64)
        else:
            payload["neighbors"][0]["score"] = float("nan")
        result.write_text(json.dumps(payload))
    snapshot = service.index(workspace_id)
    assert not [row for row in snapshot["relations"] if row.get("derived")]
    assert {row["type"] for row in snapshot["relations"]} == {"supports", "cites"}
    assert snapshot["semantic_map"]["status"] in {"partial", "unavailable"}
    assert snapshot["warnings"]


@pytest.mark.skipif(os.name != "posix", reason="Requires symlinks")
def test_semantic_result_symlink_is_not_followed(mapped, tmp_path):
    _, service, workspace_id, _, result = mapped
    outside = tmp_path / "outside.json"
    outside.write_text("private sentinel")
    result.unlink()
    result.symlink_to(outside)
    snapshot = service.index(workspace_id)
    assert snapshot["semantic_map"]["status"] == "unavailable"
    assert "private sentinel" not in json.dumps(snapshot)
    assert len(snapshot["relations"]) == 2
