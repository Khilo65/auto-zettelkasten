"""Synthetic canonical-workspace tests of the public offline calibration route."""
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

pytest.importorskip("numpy")
pytest.importorskip("igraph")
pytest.importorskip("leidenalg")

from auto_zettelkasten.files import write_json, write_yaml
from auto_zettelkasten.local_mapping import LocalMappingError
from auto_zettelkasten.mapping import calibrate_local_map, refresh_local_map
from auto_zettelkasten.mapping_calibration import stratified_holdout
from auto_zettelkasten.models import EvidenceProfile
from auto_zettelkasten.pipeline import all_workspace_note_rows
from auto_zettelkasten.profiles import save_profile
from auto_zettelkasten.workspace import initialize, load_config
from test_local_mapping_integration import Encoder


@pytest.fixture
def canonical_workspace(tmp_path):
    root = tmp_path / "synthetic-memory"
    initialize(root)
    catalogue = []
    for number in range(20):
        sid, nid = f"source-{number:02d}", f"note-{number:02d}"
        key, collection = f"KEY{number:05d}", "elections" if number < 10 else "mediation"
        path = f"02_source_memory/notes/{nid}.md"
        (root / path).write_text(f"---\nnote_id: {nid}\nsource_id: {sid}\nnote_status: analytical_atomic_note\n---\n# Synthetic {number}\n\n## Thesis\nInstitutions shape bargaining incentives in case {number}. Conflicting explanations remain.\n\n## Method\nA synthetic comparative study.\n\n## Limitations\nThis is synthetic evidence for a test.\n")
        save_profile(root / "02_source_memory/profiles", EvidenceProfile(source_id=sid, note_id=nid, context={"note_path": path}, concepts=["institutional bargaining", collection]))
        write_yaml(root / "11_state/note_metadata" / f"{nid}.yml", {"note_path": path, "frontmatter": {"source_id": sid, "note_id": nid}})
        catalogue.append({"source_id": sid, "note_id": nid, "title": f"Synthetic institutional bargaining study {number}", "zotero_key": key, "collection_keys": [collection], "identity": {"canonical_zotero_key": key, "zotero_item_keys": [key]}})
    write_yaml(root / "02_source_memory/indexes/source_catalogue.yml", {"sources": catalogue})
    return root


def build_and_freeze(workspace, tmp_path):
    built = refresh_local_map(workspace, note_rows=all_workspace_note_rows(workspace), encoder_factory=lambda _: Encoder())
    ids = [row["source_id"] for row in built["result"]["sources"] if row["chunk_ids"]]
    assert len(ids) == 20
    collections = {sid: ["elections" if int(sid[-2:]) < 10 else "mediation"] for sid in ids}
    held = set(stratified_holdout(collections))
    training = sorted(set(ids) - held)
    pairs = [(training[0], training[1], "useful"), (training[0], training[-1], "unsupported"), (min(held), training[0], "useful")]
    reviews = [{"pair": [left, right], "label": label, "reviewer_id": reviewer, "score_blinded": True} for left, right, label in pairs for reviewer in ("blind-reviewer-one", "blind-reviewer-two")]
    path = tmp_path / "frozen-score-blind-reviews.json"
    write_json(path, {"collections": collections, "reviews": reviews})
    return built, path, collections


def test_public_calibration_admits_auto_refresh_without_reencoding(canonical_workspace, tmp_path):
    workspace = canonical_workspace
    built, review_path, collections = build_and_freeze(workspace, tmp_path)
    cache = workspace / ".auto-zettelkasten" / "mapping"
    protected = {path: path.read_bytes() for path in workspace.rglob("*") if path.is_file() and not path.is_relative_to(cache)}
    result = calibrate_local_map(workspace, review_path)
    assert result["status"] == "complete"
    assert result["provider_calls"] == 0
    assert result["calibration"]["coverage"]["held_out_works"] == 4
    assert result["calibration"]["holdout_source_ids"] == stratified_holdout(collections)
    assert len(result["calibration"]["configurations"]) == 12
    assert Path(result["path"]) == cache / "calibration.json"
    encoder = Encoder()
    refreshed = refresh_local_map(workspace, options={"tuning_mode": "auto"}, encoder_factory=lambda _: encoder)["result"]
    assert refreshed["tuning"]["status"] == "calibrated"
    assert refreshed["tuning"]["calibration_identity"] == result["calibration"]["calibration_identity"]
    assert refreshed["stats"]["encoded_chunks"] == 0
    assert refreshed["stats"]["reused_chunks"] == built["result"]["stats"]["chunks"]
    assert encoder.calls == []
    assert all(path.read_bytes() == raw for path, raw in protected.items())


def test_incomplete_attempt_preserves_previous_completed_calibration(canonical_workspace, tmp_path):
    workspace = canonical_workspace
    _, review_path, _ = build_and_freeze(workspace, tmp_path)
    completed = calibrate_local_map(workspace, review_path)
    assert completed["status"] == "complete"
    completed_path = Path(completed["path"])
    original = completed_path.read_bytes()
    material = json.loads(review_path.read_text())
    material["reviews"] = material["reviews"][::2]
    incomplete_path = tmp_path / "single-review-attempt.json"
    write_json(incomplete_path, material)
    incomplete = calibrate_local_map(workspace, incomplete_path)
    assert incomplete["status"] == "incomplete"
    assert Path(incomplete["path"]).name == "calibration-last-attempt.json"
    assert incomplete["calibration"]["selected_parameters"] is None
    assert completed_path.read_bytes() == original


def test_public_resource_guard_does_not_overwrite_calibration(canonical_workspace, tmp_path, monkeypatch):
    workspace = canonical_workspace
    _, review_path, _ = build_and_freeze(workspace, tmp_path)
    completed = calibrate_local_map(workspace, review_path)
    assert completed["status"] == "complete"
    path = Path(completed["path"])
    before = path.read_bytes()
    monkeypatch.setattr(shutil, "disk_usage", lambda *_: SimpleNamespace(free=0))
    with pytest.raises(LocalMappingError, match="free-disk limit"):
        calibrate_local_map(workspace, review_path)
    assert path.read_bytes() == before


def test_calibration_cli_is_offline_and_manual_pin_is_preserved(canonical_workspace):
    from auto_zettelkasten.cli import _local_mapping_options, build_parser
    parser = build_parser()
    args = parser.parse_args(["calibrate-map", "--workspace", str(canonical_workspace), "--reviews", "/synthetic/reviews.json"])
    assert args.command == "calibrate-map"
    assert args.reviews == Path("/synthetic/reviews.json")
    build = parser.parse_args(["build-map", "--workspace", str(canonical_workspace)])
    config = load_config(canonical_workspace)
    config["local_mapping"]["tuning_mode"] = "manual"
    config["local_mapping"]["neighbors"] = 32
    preserved = _local_mapping_options(build, config)
    assert preserved["tuning_mode"] == "manual" and preserved["neighbors"] == 32
    assert build.execute_relationship_decisions is None
    assert build.relationship_decisions is None
    assert build.relationship_approval is None
    override = parser.parse_args(["build-map", "--workspace", str(canonical_workspace), "--local-tuning", "auto"])
    assert _local_mapping_options(override, config)["tuning_mode"] == "auto"
