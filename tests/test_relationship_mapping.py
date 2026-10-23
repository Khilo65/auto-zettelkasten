from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest

from auto_zettelkasten import api, cli
from auto_zettelkasten.files import write_yaml
from auto_zettelkasten.models import EvidenceProfile, MapRequest
from auto_zettelkasten.pipeline import _source_replay_request_hash
from auto_zettelkasten.profiles import save_profile
from auto_zettelkasten.relationship_mapping import (
    RelationshipMappingOptions, group_pair_requests, load_decision_endpoints,
    run_relationship_mapping,
)
from auto_zettelkasten.workspace import initialize, load_config


@pytest.fixture
def notes(tmp_path):
    initialize(tmp_path)
    rows = []
    for name, thesis in (("a", "Institutions condition election risks."),
                         ("b", "Early elections increase risk unless institutions are strong."),
                         ("c", "Peacekeeping can reduce recurrence.")):
        sid, nid = f"source-{name}", f"note-{name}"
        path = tmp_path / f"02_source_memory/notes/{name}.md"
        path.write_text(f"---\nsource_id: {sid}\nnote_id: {nid}\n---\n# {name}\n\n## Thesis\n\n{thesis}\n\n## Method and Research Design\n\nObservational evidence.\n\n## Limitations\n\nAssociational rather than causal.\n")
        relative = str(path.relative_to(tmp_path))
        save_profile(tmp_path / "02_source_memory/profiles", EvidenceProfile(
            source_id=sid, note_id=nid, concepts=["institutions"], context={"note_path": relative}))
        write_yaml(tmp_path / f"11_state/note_metadata/{nid}.yml", {
            "note_path": relative, "frontmatter": {"source_id": sid, "note_id": nid}})
        rows.append({"source_id": sid, "note_id": nid, "title": name, "note_path": relative, "zotero_key": name.upper()})
    write_yaml(tmp_path / "02_source_memory/indexes/source_catalogue.yml", {"sources": rows})
    return tmp_path, rows


def test_options_are_offline_and_separate_from_source_replay():
    off = MapRequest("/tmp/synthetic")
    preview = MapRequest("/tmp/synthetic", relationship_decisions={"backend": "jev", "max_spend_usd": "0.05"})
    assert off.relationship_decisions["backend"] == "off"
    assert preview.relationship_decisions["dry_run"] is True
    assert MapRequest.from_dict(preview.to_dict()) == preview
    assert _source_replay_request_hash(off) == _source_replay_request_hash(preview)


@pytest.mark.parametrize("value", [[], {"backend": "automatic"}, {"max_pairs": True},
                                    {"max_pairs": 0}, {"batch_size": 4}, {"apply": 1},
                                    {"max_spend_usd": "NaN"}, {"max_spend_usd": "-1"},
                                    {"api_key": "must-never-be-serialized"}])
def test_invalid_options_fail_at_boundary(value):
    with pytest.raises(ValueError):
        RelationshipMappingOptions.from_dict(value)


def test_disabled_stage_does_not_create_state(tmp_path):
    assert run_relationship_mapping(tmp_path, [])["status"] == "disabled"
    assert not list(tmp_path.iterdir())


def test_note_packet_preserves_conditions_and_exact_evidence(notes):
    root, rows = notes
    loaded = load_decision_endpoints(root, rows)
    endpoint = loaded["endpoints"]["source-b"]
    assert "unless institutions are strong" in endpoint.note_text
    assert "Associational rather than causal" in endpoint.note_text
    passage = endpoint.passages[0]
    raw = (root / rows[1]["note_path"]).read_bytes()
    assert passage.text in raw.decode()
    assert passage.content_sha256 == sha256(passage.text.encode()).hexdigest()
    assert loaded["bindings"]["source-b"]["note_sha256"] == sha256(raw).hexdigest()
    assert passage.locator["original_document_locator_verified"] is False


def test_dry_run_selects_pairs_without_network_or_note_changes(notes, monkeypatch):
    root, _ = notes
    protected = {path: path.read_bytes() for path in root.rglob("*.md")}
    monkeypatch.setattr("auto_zettelkasten.jev_batches._http_transport", lambda *_a, **_k: pytest.fail("paid dispatch"))
    result = run_relationship_mapping(root, [{"source_id": "source-a", "target_source_id": "source-b", "score": .9}],
                                      options={"backend": "jev"})
    assert result["status"] == "dry_run"
    assert result["provider_calls"] == 0
    assert result["request_count"] >= 1
    assert all(path.read_bytes() == data for path, data in protected.items())
    assert result["path"].is_file()


def test_cli_has_separate_budget_and_explicit_application():
    args = cli.build_parser().parse_args(["build-map", "--workspace", "/tmp/synthetic", "--relationship-decisions", "jev",
                                          "--max-relationship-spend-usd", ".05", "--apply-relationships"])
    options = cli._relationship_mapping_options(args, {})
    assert Decimal(options["max_spend_usd"]) == Decimal(".05")
    assert options["apply"] is True
    assert options["dry_run"] is True


def test_config_loading_defaults_and_invalid_backend(notes):
    root, _ = notes
    assert load_config(root)["relationship_decisions"]["backend"] == "off"
    config_path = root / "auto-zettelkasten.yml"
    from auto_zettelkasten.files import read_yaml
    config = read_yaml(config_path)
    config["relationship_decisions"] = {"backend": "invalid"}
    write_yaml(config_path, config)
    with pytest.raises(ValueError):
        load_config(root)


def test_grouping_never_expands_unrequested_pairs(notes):
    from auto_zettelkasten.jev_decisions import DecisionRequest
    root, rows = notes
    endpoints = load_decision_endpoints(root, rows)["endpoints"]
    requests = [DecisionRequest("synthetic", "0", endpoints["source-a"], endpoints[sid]) for sid in ("source-b", "source-c")]
    grouped = group_pair_requests(requests, 3)
    assert len(grouped) == 1
    assert grouped[0] == tuple(requests)


def test_actual_note_identity_is_checked_before_dispatch(notes, monkeypatch):
    root, rows = notes
    path = root / rows[0]['note_path']
    path.write_text(path.read_text().replace('source_id: source-a', 'source_id: another-source'))
    monkeypatch.setattr('auto_zettelkasten.jev_batches._http_transport', lambda *_a, **_k: pytest.fail('paid dispatch'))
    with pytest.raises(ValueError, match='actual note identity'):
        run_relationship_mapping(root, [{'source_id': 'source-a', 'target_source_id': 'source-b', 'score': .9}],
                                 options={'backend': 'jev', 'dry_run': False})


def test_actual_attribution_warning_survives_metadata(notes):
    root, rows = notes
    path = root / rows[0]['note_path']
    path.write_text(path.read_text().replace('note_id: note-a', 'note_id: note-a\nattribution_warning: Unresolved author identity'))
    endpoint = load_decision_endpoints(root, rows)['endpoints']['source-a']
    assert endpoint.ownership['attribution_warning'] == 'Unresolved author identity'


def test_existing_accepted_pairs_are_excluded_before_evidence_preparation(notes, monkeypatch):
    root, rows = notes
    write_yaml(root / '02_source_memory/indexes/typed_links.yml', {'relations': [{
        'source_id': 'source-a', 'target_source_id': 'source-b', 'decision_status': 'accepted',
        'relation_type': 'qualifies', 'active': True}]})
    seen = []
    real_loader = load_decision_endpoints
    def selected_loader(workspace, selected):
        seen.extend(row['source_id'] for row in selected)
        return real_loader(workspace, selected)
    monkeypatch.setattr('auto_zettelkasten.relationship_mapping.load_decision_endpoints', selected_loader)
    # An empty source selection must not expand into unrelated local candidates.
    result = run_relationship_mapping(root, [{'source_id': 'source-a', 'target_source_id': 'source-b', 'score': .9}],
                                      source_ids=[], options={'backend': 'jev'})
    assert result['candidate_count'] == 0
    assert seen == []
    assert result['reused_pair_count'] >= 1


@pytest.mark.parametrize('status', ['persistence_failed', 'disabled', 'billing_uncertain', 'invalid_response'])
def test_dispatch_failures_are_reported_as_partial(notes, monkeypatch, status):
    root, _ = notes
    def assess(self, request, *, dry_run=False):
        return {'status': 'dry_run' if dry_run else status, 'provider_calls': 0}
    monkeypatch.setattr('auto_zettelkasten.jev_batches.JevBatchClient.assess', assess)
    result = run_relationship_mapping(root, [{'source_id': 'source-a', 'target_source_id': 'source-b', 'score': .9}],
                                      options={'backend': 'jev', 'dry_run': False})
    assert result['status'] == 'partial'
    assert result['coverage_partial'] is True


@pytest.mark.parametrize('options', [{'batch_size': 3}, {'packet_format': 'compact'}])
def test_unsuccessful_experimental_packaging_stays_blocked(tmp_path, options):
    result = run_relationship_mapping(tmp_path, [], options={'backend': 'jev', 'dry_run': False, **options})
    assert result['status'] == 'blocked'
    assert result['provider_calls'] == 0
    assert not list(tmp_path.iterdir())


def test_application_requires_private_copy_before_dispatch(notes, monkeypatch):
    root, _ = notes
    monkeypatch.setattr('auto_zettelkasten.jev_batches._http_transport', lambda *_a, **_k: pytest.fail('paid dispatch'))
    with pytest.raises(ValueError, match='private copy'):
        run_relationship_mapping(root, [], options={'backend': 'jev', 'dry_run': False, 'apply': True})


def test_budget_blocked_pass_is_unfinished_and_later_pass_advances(notes, monkeypatch):
    root, _ = notes
    seen = []
    def blocked(self, batch, *, dry_run=False):
        pair = tuple(sorted((batch.requests[0].endpoint_a.source_id, batch.requests[0].endpoint_b.source_id)))
        if not dry_run:
            seen.append(pair)
        return {"status": "dry_run" if dry_run else "blocked", "provider_calls": 0}
    monkeypatch.setattr("auto_zettelkasten.jev_batches.JevBatchClient.assess", blocked)
    opts = {"backend": "jev", "dry_run": False, "max_pairs": 1, "per_source_cap": 1}
    edges = [{"source_id": "source-a", "target_source_id": sid, "score": .9} for sid in ("source-b", "source-c")]
    protected = {path: path.read_bytes() for path in root.rglob("*.md")}
    first = run_relationship_mapping(root, edges, options=opts)
    second = run_relationship_mapping(root, edges, options=opts)
    assert len(seen) == 2 and seen[0] != seen[1]
    assert first["candidate_queue"]["unfinished"] is True
    assert second["candidate_queue"]["counts"]["unexamined"] >= 2
    assert first["coverage_partial"] and second["status"] == "partial"
    assert all(path.read_bytes() == data for path, data in protected.items())


def test_dry_previews_do_not_consume_queue_and_real_note_edits_invalidate(notes, monkeypatch):
    from auto_zettelkasten.candidate_queue import CandidateQueue, queue_scope
    from auto_zettelkasten.local_tools import load_local_retrieval
    from auto_zettelkasten.mapping_candidates import prepare_mapping_candidates
    root, rows = notes
    monkeypatch.setattr("auto_zettelkasten.jev_batches._http_transport", lambda *_a, **_k: pytest.fail("paid dispatch"))
    edge = [{"source_id": "source-a", "target_source_id": "source-b", "score": .9}]
    first = run_relationship_mapping(root, edge, options={"backend": "jev", "max_pairs": 1})
    second = run_relationship_mapping(root, edge, options={"backend": "jev", "max_pairs": 1})
    assert first["preview"][0]["cache_key"] == second["preview"][0]["cache_key"]
    loaded = load_local_retrieval(root, library_id="0", infer_scope=True)
    kwargs = {"persist_queue": True, "decision_contract": {"test_policy": "v1"}, "loaded": loaded, "max_pairs": 1}
    prepared = prepare_mapping_candidates(root, edge, **kwargs)
    q = CandidateQueue(root, queue_scope(loaded["status"].get("instance_id"), loaded["status"].get("library_id"), None))
    selected = prepared["candidates"][0]
    q.record(selected, "negative")
    same = prepare_mapping_candidates(root, [{**edge[0], "score": .8}], **kwargs)
    assert q.summary()["counts"]["negative"] == 1
    path = root / rows[0]["note_path"]
    path.write_text(path.read_text().replace("Institutions condition", "Institutional design conditions"))
    changed = prepare_mapping_candidates(root, edge, **kwargs)
    assert changed["queue"]["counts"].get("negative", 0) == 0
    assert changed["queue"]["historical_count"] >= 1


def test_candidate_resource_bound_reports_unfinished_without_dispatch(notes, monkeypatch):
    from auto_zettelkasten.retrieval import CandidateResourceError
    root, _ = notes
    def too_large(*args, **kwargs):
        raise CandidateResourceError("candidate pool exceeds the bounded queue; reduce source scope")
    monkeypatch.setattr("auto_zettelkasten.mapping_candidates.prepare_mapping_candidates", too_large)
    monkeypatch.setattr("auto_zettelkasten.jev_batches._http_transport", lambda *_a, **_k: pytest.fail("paid dispatch"))
    result = run_relationship_mapping(root, [], options={"backend": "jev", "dry_run": False})
    assert result["status"] == "partial" and result["coverage_partial"]
    assert result["candidate_queue"]["unfinished"] and result["provider_calls"] == 0
