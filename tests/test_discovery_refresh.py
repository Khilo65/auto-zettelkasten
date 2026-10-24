"""Offline cache-only entry point, using synthetic canonical source notes."""
import json
import socket

import pytest

pytest.importorskip("numpy")
pytest.importorskip("igraph")
pytest.importorskip("leidenalg")

from auto_zettelkasten import local_mapping
from auto_zettelkasten.cli import build_parser, main
from auto_zettelkasten.discovery_refresh import refresh_cached_discovery
from auto_zettelkasten.local_mapping import LocalMappingError
from auto_zettelkasten.mapping import refresh_local_map
from auto_zettelkasten.pipeline import all_workspace_note_rows
from test_local_mapping_integration import Encoder
from test_mapping_calibration_integration import canonical_workspace


def build(workspace):
    return refresh_local_map(workspace, note_rows=all_workspace_note_rows(workspace), encoder_factory=lambda _: Encoder())["result"]


def protected(workspace):
    return {path: path.read_bytes() for path in workspace.rglob("*") if path.is_file() and ".auto-zettelkasten" not in path.relative_to(workspace).parts}


def test_cached_scope_refresh_and_cost_preview_never_encode_or_dispatch(canonical_workspace, monkeypatch):
    root = canonical_workspace
    prior = build(root)
    before = protected(root)
    def forbidden(*args, **kwargs):
        pytest.fail("Offline discovery must not load an encoder or dispatch a request")
    monkeypatch.setattr(local_mapping, "_LocalMiniLM", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    from auto_zettelkasten.jev_batches import JevBatchClient
    from auto_zettelkasten.jev_decisions import DecisionStore
    monkeypatch.setattr(DecisionStore, "reserve", forbidden)
    original = JevBatchClient.assess
    def assess(self, batch, *, dry_run=True):
        assert dry_run is True
        return original(self, batch, dry_run=dry_run)
    monkeypatch.setattr(JevBatchClient, "assess", assess)
    report = refresh_cached_discovery(root, preview_decisions=True, max_pairs=6)
    result = json.loads((root / ".auto-zettelkasten/mapping/result.json").read_text())
    assert report["provider_calls"] == 0 and report["cost_usd"] == "0"
    assert result["stats"]["encoder_loads"] == result["stats"]["encoded_chunks"] == 0
    assert result["identity"]["encoder"] == prior["identity"]["encoder"]
    assert result["chunks"] == prior["chunks"]
    assert report["scope_suggestions"]["within_collection"] > 0
    assert report["scope_suggestions"]["whole_library"] > 0
    assert report["decision_preview"]["paid_dispatch_enabled"] is False
    assert report["decision_preview"]["provider_calls"] == 0
    assert report["decision_preview"]["budget_usd"] == "0"
    from pathlib import Path
    assert Path(report["decision_preview"]["path"]).is_file()
    assert report["decision_preview"]["request_count"] <= 6
    assert report["decision_preview"]["ceiling_covers_all_requests"] is True
    assert protected(root) == before


def test_stale_notes_preserve_saved_map_without_encoding(canonical_workspace, monkeypatch):
    root = canonical_workspace
    result = build(root)
    path = root / ".auto-zettelkasten/mapping/result.json"
    prior = path.read_bytes()
    note = root / result["sources"][0]["note_path"]
    note.write_text(note.read_text() + "\nChanged claim.\n")
    monkeypatch.setattr(local_mapping, "_LocalMiniLM", lambda *_: pytest.fail("encoder loaded"))
    with pytest.raises(LocalMappingError, match="stale"):
        refresh_cached_discovery(root)
    assert path.read_bytes() == prior


def test_corrupt_vectors_preserve_saved_map(canonical_workspace):
    root = canonical_workspace
    build(root)
    path = root / ".auto-zettelkasten/mapping/result.json"
    prior = path.read_bytes()
    checkpoint = next((path.parent / "chunks").rglob("*.f32"))
    checkpoint.write_bytes(b"broken")
    with pytest.raises(LocalMappingError):
        refresh_cached_discovery(root)
    assert path.read_bytes() == prior


def test_changed_source_cache_rejects_stale_vectors_even_when_notes_are_unchanged(canonical_workspace):
    import hashlib
    import yaml
    root = canonical_workspace
    metadata = root / "11_state/note_metadata/note-00.yml"
    payload = yaml.safe_load(metadata.read_text())
    payload["frontmatter"].update(zotero_item_key="KEY00000", inspected_content_hash="a" * 64)
    metadata.write_text(yaml.safe_dump(payload))
    checkpoint = root / "11_state/runs/synthetic-source/items/KEY00000"
    checkpoint.mkdir(parents=True)
    manifest = checkpoint / "frozen_content.yml"
    def source(text):
        (checkpoint / "source.txt").write_text(text)
        manifest.write_text(yaml.safe_dump({"checkpoint_version": "1", "content_hash": "a" * 64,
            "text_hash": hashlib.sha256(text.encode()).hexdigest(), "source_scope": "full_document"}))
    source("Chapter 1\nInitial substantive argument.\n")
    build(root)
    path = root / ".auto-zettelkasten/mapping/result.json"
    previous = path.read_bytes()
    note = root / "02_source_memory/notes/note-00.md"
    note_before, metadata_before = note.read_bytes(), metadata.read_bytes()
    source("Chapter 1\nA corrected and different substantive extraction.\n")
    with pytest.raises(LocalMappingError, match="stale"):
        refresh_cached_discovery(root)
    assert path.read_bytes() == previous
    assert note.read_bytes() == note_before and metadata.read_bytes() == metadata_before


def test_membership_changes_refresh_discovery_without_reembedding(canonical_workspace):
    import yaml
    root = canonical_workspace
    original = build(root)
    first = refresh_cached_discovery(root)
    path = root / "02_source_memory/indexes/source_catalogue.yml"
    catalogue = yaml.safe_load(path.read_text())
    for source in catalogue["sources"]:
        source["collection_keys"] = ["one-collection"]
    path.write_text(yaml.safe_dump(catalogue))
    second = refresh_cached_discovery(root)
    current = json.loads((root / ".auto-zettelkasten/mapping/result.json").read_text())
    assert first["scope_coverage"]["collections"] == 2
    assert second["scope_coverage"]["collections"] == 1
    assert current["chunks"] == original["chunks"]
    assert second["stats"]["encoded_chunks"] == 0


def test_current_metadata_paths_bind_relocated_private_notes(canonical_workspace):
    import hashlib
    import yaml
    root = canonical_workspace
    result = build(root)
    source = result["sources"][0]
    original = root / source["note_path"]
    moved = original.parent / "synthetic-collection" / original.name
    moved.parent.mkdir()
    original.rename(moved)
    metadata = root / source["metadata_path"]
    payload = yaml.safe_load(metadata.read_text())
    payload["note_path"] = str(moved.relative_to(root))
    metadata.write_text(yaml.safe_dump(payload))
    source["note_path"] = payload["note_path"]
    source["metadata_file_sha256"] = hashlib.sha256(metadata.read_bytes()).hexdigest()
    saved = root / ".auto-zettelkasten/mapping/result.json"
    saved.write_text(json.dumps(result))
    before = moved.read_bytes()
    report = refresh_cached_discovery(root)
    assert report["stats"]["encoded_chunks"] == 0
    assert moved.read_bytes() == before


def test_cli_has_no_paid_switch_and_preserves_validation(canonical_workspace, capsys):
    root = canonical_workspace
    build(root)
    assert main(["refresh-discovery", "--workspace", str(root)]) == 0
    assert json.loads(capsys.readouterr().out)["provider_calls"] == 0
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["refresh-discovery", "--workspace", str(root), "--execute"])
    args = parser.parse_args(["build-map", "--workspace", str(root), "--discovery-scope", "collection_and_library"])
    assert args.discovery_scope == "collection_and_library"
