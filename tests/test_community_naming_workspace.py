from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json

import pytest

from auto_zettelkasten import cli
from auto_zettelkasten.community_labeling import bind_naming_corpus
from auto_zettelkasten.community_naming_pipeline import CommunityNamingCache, prepare_two_stage_naming
from auto_zettelkasten import community_naming_workspace as naming
from auto_zettelkasten.documents import DocumentService


@pytest.fixture
def prepared(tmp_path):
    root = tmp_path / "workspace"
    (root / "notes").mkdir(parents=True)
    for number, title in ((1, "Monetary institutions"), (2, "Public finance")):
        (root / f"notes/{number}.md").write_text(
            f"---\nnote_id: n{number}\ntitle: {title}\n---\n# {title}\n\n## Thesis\n"
            f"{title} affect public policy.\n\n## Limitations\nThese are bounded observations.\n")
    groups = [{"community_id": f"c{i}", "source_ids": [f"s{i}"], "display_label": f"old {i}"} for i in (1, 2)]
    mapping = {"sources": [{"source_id": f"s{i}", "note_id": f"n{i}", "note_path": f"notes/{i}.md"} for i in (1, 2)],
               "communities": groups,
               "community_scales": [{"scale": "broad", "communities": [{"community_id": "wide", "source_ids": ["s1", "s2"], "display_label": "Broad subjects"}]},
                                    {"scale": "default", "communities": deepcopy(groups)},
                                    {"scale": "fine", "communities": deepcopy(groups)}],
               "identity": {"model": "unchanged", "resolution": 1, "seed": 0},
               "neighbors": [{"source_id": "s1", "target_source_id": "s2", "score": .8}],
               "accepted_relationships": [{"type": "supports", "source_id": "s1", "target_source_id": "s2"}],
               "stats": {"sources": 2}}
    path = root / ".auto-zettelkasten/mapping/result.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(mapping))
    service = DocumentService(journal_root=tmp_path / "operations", override_root=tmp_path / "overrides")
    opened = service.open(root)
    corpus = bind_naming_corpus(mapping, service.recall_corpus(opened["id"]))
    preview = prepare_two_stage_naming(groups, corpus)
    cache_path = tmp_path / "naming.sqlite3"
    cache = CommunityNamingCache(cache_path)
    drafts = {"labels": [{"community_id": f"c{i}", "title": title, "scope": title + " and policy."} for i, title in ((1, "Money"), (2, "Finance"))]}
    final = {"labels": [{"community_id": "c1", "title": "Monetary institutions"}, {"community_id": "c2", "title": "Public finance"}]}
    provenance = {"origin": "imported_saved", "provider": "openai", "model": "gpt-6-luna",
                  "source_template_version": "synthetic-historical-template", "evidence_identity": preview["evidence_identity"],
                  "local_requests": {f"c{i}": {"request_sha256": "a" * 64, "response_sha256": "b" * 64} for i in (1, 2)},
                  "reconciliation_request_sha256": "c" * 64, "reconciliation_response_sha256": "d" * 64,
                  "source_artifact_sha256": "e" * 64}
    cache.import_stage_results(preview, drafts, final, provenance)
    service.close()
    return root, path, mapping, cache_path, tmp_path / "overrides"


def run(prepared, **kwargs):
    root, _, _, cache_path, overrides = prepared
    return naming.community_names_manifest(root, cache_path=cache_path, override_root=overrides, **kwargs)


def test_apply_saved_names_preserves_all_topology_and_notes(prepared):
    root, path, original, _, _ = prepared
    notes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (root / "notes").glob("*.md")}
    report = run(prepared, apply=True)
    current = json.loads(path.read_text())
    assert report["status"] == "applied"
    assert report["provider_calls"] == 0 and report["jev_calls"] == 0
    assert report["applied_scales"] == ["default", "fine"]
    assert [row["display_label"] for row in current["communities"]] == ["Monetary institutions", "Public finance"]
    assert current["community_scales"][0] == original["community_scales"][0]
    assert current["community_scales"][1]["communities"] == current["communities"]
    assert current["community_scales"][2]["communities"] == current["communities"]
    for field in ("sources", "identity", "neighbors", "accepted_relationships", "stats"):
        assert current[field] == original[field]
    assert notes == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in notes}
    assert report["cache"]["imported_reuse_count"] == 3
    assert report["cache"]["exact_request_cache_hits"] == 0


def test_preview_does_not_apply_and_second_apply_is_idempotent(prepared):
    _, path, _, _, _ = prepared
    before = path.read_bytes()
    assert run(prepared)["status"] == "cached"
    assert path.read_bytes() == before
    run(prepared, apply=True)
    applied = path.read_bytes()
    report = run(prepared, apply=True)
    assert report["status"] == "unchanged" and report["provider_calls"] == 0
    assert path.read_bytes() == applied


def test_fine_view_reuses_default_cache(prepared):
    report = run(prepared, scale="fine", apply=True)
    assert report["canonical_scale"] == "default"
    assert report["provider_calls"] == 0


def test_stale_notes_do_not_apply_old_names_or_call_reader(prepared):
    root, path, _, _, _ = prepared
    with (root / "notes/1.md").open("a") as handle:
        handle.write("\nNew substantive evidence.\n")
    before = path.read_bytes()
    class NoReader:
        def name_community_stage(self, *args, **kwargs):
            pytest.fail("Cache misses must not dispatch by default")
    report = run(prepared, apply=True, reader=NoReader())
    assert not report["applied"] and report["provider_calls"] == 0
    assert report["cache"]["final"] is None
    assert path.read_bytes() == before


@pytest.mark.parametrize("change", ["graph", "note"])
def test_recheck_under_lock_rejects_concurrent_changes(prepared, monkeypatch, change):
    root, path, original, _, _ = prepared
    @contextmanager
    def changed(_):
        if change == "graph":
            latest = deepcopy(original)
            latest["stats"]["sources"] = 99
            path.write_text(json.dumps(latest))
        else:
            with (root / "notes/1.md").open("a") as handle:
                handle.write("\nChanged concurrently.\n")
        yield
    monkeypatch.setattr(naming, "_exclusive_cache", changed)
    with pytest.raises(ValueError, match="changed"):
        run(prepared, apply=True)
    assert [row["display_label"] for row in json.loads(path.read_text())["communities"]] == ["old 1", "old 2"]


def test_cli_is_off_by_default_and_old_command_remains(prepared, monkeypatch, capsys):
    calls = []
    def cached(workspace, **kwargs):
        calls.append(kwargs)
        return {"provider_calls": 0, "status": "cached"}
    monkeypatch.setattr(naming, "community_names_manifest", cached)
    assert cli.main(["community-names", "--workspace", str(prepared[0]), "--apply"]) == 0
    assert calls[0]["enabled"] is False and calls[0]["allow_cloud"] is False
    assert calls[0]["apply"] is True
    assert json.loads(capsys.readouterr().out)["provider_calls"] == 0
    parsed = cli.build_parser().parse_args(["community-name", "--workspace", str(prepared[0]), "--community-id", "c1"])
    assert parsed.command == "community-name"
