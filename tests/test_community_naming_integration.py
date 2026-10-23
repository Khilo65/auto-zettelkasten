"""Synthetic workspace publishing and stale-evidence naming boundaries."""
import json
from pathlib import Path
import sqlite3

import pytest

from auto_zettelkasten.mapping import community_name_manifest, refresh_local_map
from auto_zettelkasten.pipeline import all_workspace_note_rows
from test_local_mapping_integration import Encoder, workspace


class SourceReader:
    name = "deepseek"
    model = "deepseek-v4-flash"
    capabilities = {"capability_identity": "synthetic-source-reader"}

    def __init__(self):
        self.calls = 0
        self.on_call = lambda: None

    def community_name_input_budget(self):
        return 8192

    def community_name_output_tokens(self):
        return 128

    def name_community(self, *_):
        self.calls += 1
        self.on_call()
        return {"title": "Institutional designs and competing explanations"}


def setup(workspace):
    built = refresh_local_map(workspace, note_rows=all_workspace_note_rows(workspace), encoder_factory=lambda _: Encoder())
    community = built["result"]["communities"][0]["community_id"]
    return built, community


def prepare(workspace, community, reader):
    return community_name_manifest(workspace, community, reader=reader, input_price_per_million="1", output_price_per_million="2", pricing_identity="synthetic-only-pricing-v1")


def execute(workspace, community, reader, preview):
    return community_name_manifest(workspace, community, reader=reader, input_price_per_million="1", output_price_per_million="2", pricing_identity="synthetic-only-pricing-v1", execute=True, allow_cloud=True, approval={"approved": True, "budget_scope": "community_naming", "request_digest": preview["preview"]["request_digest"], "approval_id": "synthetic-community", "max_spend_usd": "1"})


def test_generation_publishes_only_derived_label_and_cache(workspace):
    built, community = setup(workspace)
    reader = SourceReader()
    preview = prepare(workspace, community, reader)
    cache = workspace / ".auto-zettelkasten" / "mapping"
    protected = {path: path.read_bytes() for path in workspace.rglob("*") if path.is_file() and not path.is_relative_to(cache)}
    before = json.loads(built["path"].read_text())
    result = execute(workspace, community, reader, preview)
    assert result["status"] == "generated"
    assert result["provider_calls"] == 1
    after = json.loads(built["path"].read_text())
    group = next(row for row in before["communities"] if row["community_id"] == community)
    group["display_label"] = "Institutional designs and competing explanations"
    group["naming_identity"] = preview["preview"]["request_digest"]
    assert after == before
    assert all(path.read_bytes() == original for path, original in protected.items())
    assert (cache / "community-names.sqlite3").exists()
    assert execute(workspace, community, reader, preview)["status"] == "cached"
    assert reader.calls == 1


def test_changed_note_blocks_naming_before_dispatch(workspace):
    built, community = setup(workspace)
    reader = SourceReader()
    preview = prepare(workspace, community, reader)
    result_before = built["path"].read_bytes()
    note = workspace / built["result"]["sources"][0]["note_path"]
    note.write_text(note.read_text() + "\nNew source evidence.\n")
    with pytest.raises(ValueError, match="evidence changed"):
        execute(workspace, community, reader, preview)
    assert reader.calls == 0
    assert built["path"].read_bytes() == result_before
    assert not (workspace / ".auto-zettelkasten/mapping/community-names.sqlite3").exists()


def test_concurrent_evidence_change_keeps_title_cached_without_publication(workspace):
    built, community = setup(workspace)
    reader = SourceReader()
    preview = prepare(workspace, community, reader)
    result_before = built["path"].read_bytes()
    note = workspace / built["result"]["sources"][0]["note_path"]
    reader.on_call = lambda: note.write_text(note.read_text() + "\nConcurrent source edit.\n")
    with pytest.raises(ValueError, match="evidence changed while naming"):
        execute(workspace, community, reader, preview)
    assert reader.calls == 1
    assert built["path"].read_bytes() == result_before
    with sqlite3.connect(workspace / ".auto-zettelkasten/mapping/community-names.sqlite3") as connection:
        assert connection.execute("SELECT status FROM naming_requests").fetchone()[0] == "complete"
