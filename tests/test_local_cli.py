import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from auto_zettelkasten import api, cli
from auto_zettelkasten.models import LiteratureMappingPolicy, NavigationPolicy


def candidate(**updates):
    return {"left_source_id": "a", "right_source_id": "b", "left_source_title": "A", "right_source_title": "B",
            "rank": 1, "score": 0.5, "why_compare": "Shared mechanism.", "discovery_route": "deterministic_local_ranked",
            "discovery_provenance": [{"kind": "text", "tokens": ["mechanism"]}], **updates}


def test_local_candidates_have_separate_rank_independent_receipts():
    args = dict(provider="ollama", model="test", question=None, policy=LiteratureMappingPolicy(),
                navigation=NavigationPolicy(), comparison_collection_keys=(), source_set=None)
    legacy = api._build_map_receipt_identity(**args)
    local = api._build_map_receipt_identity(**args, ranked_candidates=[candidate()])
    assert local != legacy
    assert api._build_map_receipt_identity(**args, ranked_candidates=[]) != legacy
    assert api._build_map_receipt_identity(**args, ranked_candidates=[candidate(rank=8, score=0.1, why_compare="Reordered.")]) == local
    assert api._build_map_receipt_identity(**args, ranked_candidates=[candidate(discovery_provenance=[{"kind": "text", "tokens": ["changed"]}])]) != local


def test_malformed_shortlist_fails_before_any_workspace_or_provider_work(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "resolve_workspace", lambda *args: pytest.fail("invalid input reached workspace"))
    with pytest.raises(ValueError):
        api.build_map(tmp_path, ranked_candidates=[{**candidate(), "decision": "relationship"}])


@pytest.mark.parametrize("payload", [{"candidates": [candidate()]}, [candidate()], {"candidates": []}])
def test_local_candidate_json_reads_prepared_envelope_and_empty_selection(tmp_path, payload):
    path = tmp_path / "shortlist.json"
    path.write_text(json.dumps(payload))
    assert cli._read_local_candidates(path) == (payload["candidates"] if isinstance(payload, dict) else payload)


@pytest.mark.parametrize("payload", [{}, {"candidates": "bad"}, [candidate()] * 257])
def test_local_candidate_json_rejects_wrong_shape_and_unbounded_rows(tmp_path, payload):
    path = tmp_path / "shortlist.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        cli._read_local_candidates(path)


def test_local_candidate_json_rejects_oversized_input(tmp_path):
    path = tmp_path / "shortlist.json"
    path.write_bytes(b" " * (4 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="4 MiB"):
        cli._read_local_candidates(path)


def test_offline_commands_use_only_bounded_local_adapters(tmp_path, monkeypatch, capsys):
    calls = []
    tools = ModuleType("auto_zettelkasten.local_tools")
    def scan(workspace, **kwargs):
        calls.append(("scan", kwargs))
        return {"status": "inspected", "provider_calls": 0}
    def load(workspace, **kwargs):
        calls.append(("load", kwargs))
        return {"index": SimpleNamespace(candidate_pairs=lambda **opts: [candidate()],
                                          recall=lambda query, **opts: {"notes": [], "relationships": [], "chars": 0}),
                "status": {"bibliography": "not_examined"}, "relationships": []}
    tools.scan_local_collection = scan
    tools.load_local_retrieval = load
    monkeypatch.setitem(sys.modules, tools.__name__, tools)
    common = ["--workspace", str(tmp_path), "--instance-id", "desktop-one"]
    assert cli.main(["local-discovery", *common, "--collection", "COLL0001", "--limit", "4", "--start", "8"]) == 0
    assert json.loads(capsys.readouterr().out)["provider_calls"] == 0
    assert calls[-1][1]["limit"] == 4 and calls[-1][1]["start"] == 8
    assert cli.main(["candidates", *common, "--max-pairs", "4"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["candidates"] == [candidate()] and output["coverage"]["bibliography"] == "not_examined"
    assert cli.main(["recall", *common, "--query", "mechanism", "--max-notes", "2"]) == 0
    assert json.loads(capsys.readouterr().out)["provider_calls"] == 0


def test_build_map_cli_only_passes_local_candidates_when_requested(tmp_path, monkeypatch, capsys):
    path = tmp_path / "shortlist.json"
    path.write_text(json.dumps({"candidates": [candidate()]}))
    monkeypatch.setattr(cli, "load_config", lambda *args: {})
    calls = []
    def build(workspace, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(to_dict=lambda: {"status": "built", "artifacts": [], "metadata": {}})
    monkeypatch.setattr(cli, "build_map", build)
    common = ["build-map", "--workspace", str(tmp_path), "--provider", "ollama", "--model", "test"]
    assert cli.main(common) == 0
    capsys.readouterr()
    assert "ranked_candidates" not in calls[-1]
    assert cli.main([*common, "--local-candidates", str(path)]) == 0
    assert calls[-1]["ranked_candidates"] == [candidate()]
