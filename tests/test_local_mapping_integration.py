"""Synthetic tests of public entry points and the existing mapper completion path."""
from hashlib import sha256
from pathlib import Path
import re

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("igraph")
pytest.importorskip("leidenalg")

from auto_zettelkasten import api, local_mapping
from auto_zettelkasten.files import write_yaml
from auto_zettelkasten.mapping import refresh_local_map
from auto_zettelkasten.models import EvidenceProfile, MapRequest
from auto_zettelkasten.pipeline import _source_replay_request_hash, all_workspace_note_rows, rebuild_map
from auto_zettelkasten.profiles import save_profile
from auto_zettelkasten.workspace import initialize


class Tokenizer:
    def num_special_tokens_to_add(self, pair=False):
        return 2

    def __call__(self, text, add_special_tokens=False, **kwargs):
        offsets = [(match.start(), match.end()) for match in re.finditer(r'\S+', text)]
        return {'input_ids': list(range(len(offsets) + (2 if add_special_tokens else 0))),
                'offset_mapping': offsets}


class Encoder:
    dimension = 4
    tokenizer = Tokenizer()
    identity = {'dimension': 4, 'model_name': 'synthetic-encoder-v1'}
    model_load_seconds = 0.0

    def __init__(self, *_):
        self.calls = []

    def encode(self, texts):
        self.calls.extend(texts)
        return np.array([[byte + 1 for byte in sha256(text.encode()).digest()[:4]] for text in texts], dtype=np.float32)


def note(root, label, text):
    path = root / '02_source_memory' / 'notes' / f'{label}.md'
    path.write_text(f'---\nnote_id: note-{label}\nsource_id: source-{label}\nnote_status: analytical_atomic_note\n---\n# Findings\n{text}\n')
    return path


@pytest.fixture
def workspace(tmp_path):
    initialize(tmp_path)
    note(tmp_path, 'a', 'Institutional coordination changes participation.')
    note(tmp_path, 'b', 'Bargaining institutions organize credible commitments.')
    note(tmp_path, 'c', 'Imaging and spectroscopy measure bacterial responses.')
    write_yaml(tmp_path / '03_literature_synthesis/relationship_registry.yml', {'relations': [{'id': 'accepted-before', 'decision_status': 'accepted'}]})
    (tmp_path / 'personal-override.md').write_text('Personal interpretation remains untouched.\n')
    return tmp_path


def test_public_default_build_has_no_provider_calls_or_note_projection(workspace, monkeypatch):
    before = {str(path): path.read_bytes() for path in workspace.rglob('*') if path.is_file()}
    monkeypatch.setattr(local_mapping, '_LocalMiniLM', Encoder)
    monkeypatch.setattr(api, 'provider_from_name', lambda *args, **kwargs: pytest.fail('provider called'))
    result = api.build_map(workspace)
    assert result.metadata['mapping_backend'] == 'local'
    assert result.metadata['literature_map']['provider_calls'] == 0
    derived = result.metadata['local_mapping']
    assert derived['identity']['clustering']['clustering'] == 'leiden'
    assert derived['relationship_status'] == 'derived_suggestions_only'
    assert len(derived['sources']) == 3
    assert all(row['coverage']['input']['source_scope'] == 'note_only' for row in derived['sources'])
    assert (workspace / '.auto-zettelkasten/mapping/result.json').is_file()
    assert all(Path(path).read_bytes() == payload for path, payload in before.items())


def test_catalogue_only_refresh_and_explicit_api_rows_share_cache_without_inventing_scope(workspace, monkeypatch):
    entries = []
    for label in 'abc':
        sid, nid, key = f'source-{label}', f'note-{label}', label.upper()
        note_path = f'02_source_memory/notes/{label}.md'
        save_profile(workspace / '02_source_memory/profiles', EvidenceProfile(
            source_id=sid, note_id=nid, context={'note_path': note_path}, concepts=['synthetic institutions']))
        entries.append({'source_id': sid, 'note_id': nid, 'title': f'Synthetic {label}', 'zotero_key': key,
                        'identity': {'canonical_zotero_key': key, 'zotero_item_keys': [key, f'{key}ALIAS'],
                                     'doi': f'10.1000/synthetic-{label}'}})
        write_yaml(workspace / '11_state/note_metadata' / f'{nid}.yml', {
            'note_path': note_path, 'frontmatter': {'source_id': sid, 'note_id': nid}})
    write_yaml(workspace / '02_source_memory/indexes/source_catalogue.yml', {'sources': entries})
    before = {str(path): path.read_bytes() for path in workspace.rglob('*') if path.is_file()}
    monkeypatch.setattr(api, 'provider_from_name', lambda *args, **kwargs: pytest.fail('provider called'))
    direct = refresh_local_map(workspace, encoder_factory=lambda _: Encoder())
    assert len(direct['result']['sources']) == 3
    assert all(row['identity']['scope'] == 'workspace' for row in direct['result']['sources'])
    assert all('library_id' not in row['identity'] for row in direct['result']['sources'])
    assert direct['inputs']['projection_rows'][0]['catalogue_identity'] == entries[0]['identity']
    monkeypatch.setattr(local_mapping, '_LocalMiniLM', Encoder)
    explicit = api.build_map(workspace).metadata['local_mapping']
    assert explicit['stats']['encoded_chunks'] == 0
    assert explicit['stats']['reused_chunks'] == direct['result']['stats']['chunks']
    assert explicit['communities'] == direct['result']['communities']
    assert all(Path(path).read_bytes() == payload for path, payload in before.items())


def test_public_repeat_and_incremental_change_reuse_unchanged_sources(workspace, monkeypatch):
    encoders = []

    def create(*args):
        value = Encoder()
        encoders.append(value)
        return value

    monkeypatch.setattr(local_mapping, '_LocalMiniLM', create)
    first = api.build_map(workspace).metadata['local_mapping']
    repeated = api.build_map(workspace).metadata['local_mapping']
    assert repeated['stats']['encoded_chunks'] == 0
    assert repeated['stats']['reused_chunks'] == first['stats']['chunks']
    assert not encoders[-1].calls
    assert repeated['communities'] == first['communities']
    note(workspace, 'a', 'Revised institutions alter cooperation and participation.')
    changed = api.build_map(workspace).metadata['local_mapping']
    assert changed['stats']['encoded_chunks'] > 0
    unchanged_ids = {chunk for row in first['sources'] if row['source_id'] != 'source-a' for chunk in row['chunk_ids']}
    assert unchanged_ids <= {chunk for row in changed['sources'] for chunk in row['chunk_ids']}
    assert changed['stats']['reused_chunks'] >= len(unchanged_ids)


def test_explicit_empty_scope_does_not_expand_to_workspace(workspace, monkeypatch):
    monkeypatch.setattr(local_mapping, '_LocalMiniLM', lambda *args: pytest.fail('empty scope loaded encoder'))
    result = api.build_map(workspace, source_set={'source_ids': []})
    assert result.metadata['local_mapping']['sources'] == []


def test_ordinary_mapper_completion_uses_same_derived_route(workspace, monkeypatch):
    monkeypatch.setattr(local_mapping, '_LocalMiniLM', Encoder)
    rows = all_workspace_note_rows(workspace)
    result = rebuild_map(workspace, source_set={'source_ids': [row['source_id'] for row in rows]},
                         note_rows=rows, terminal_rows=[], items=[], run_id='synthetic-local', question=None,
                         request=MapRequest(workspace), reasoner=object())
    assert result['cluster_map']['status'] == 'derived_local'
    assert result['literature_packet']['synthesis_call_count'] == 0
    assert result['gap_map']['status'] == 'not_adjudicated'
    assert workspace / '.auto-zettelkasten/mapping/result.json' in result['paths']
    assert (workspace / '02_source_memory/indexes/source_catalogue.yml').is_file()


def test_changed_inputs_do_not_publish_stale_map(workspace):
    rows = all_workspace_note_rows(workspace)
    first = refresh_local_map(workspace, note_rows=rows, encoder_factory=lambda _: Encoder())
    before = first['path'].read_bytes()
    note(workspace, 'a', 'Changed input for the next explicit refresh.')

    class ConcurrentEdit(Encoder):
        def encode(self, texts):
            note(workspace, 'b', 'Concurrent user edit while embeddings run.')
            return super().encode(texts)

    with pytest.raises(RuntimeError, match='inputs changed'):
        refresh_local_map(workspace, note_rows=all_workspace_note_rows(workspace), encoder_factory=lambda _: ConcurrentEdit())
    assert first['path'].read_bytes() == before
    # The OS lock inode persists, but an interrupted build releases ownership.
    assert refresh_local_map(workspace, note_rows=all_workspace_note_rows(workspace),
                             encoder_factory=lambda _: Encoder())['result']['sources']


def test_mapping_settings_do_not_regenerate_existing_source_notes(workspace):
    local = MapRequest(workspace, mapping_backend='local')
    legacy = MapRequest(workspace, mapping_backend='llm')
    assert _source_replay_request_hash(local) == _source_replay_request_hash(legacy)


def test_default_estimate_has_zero_paid_graph_calls(workspace):
    result = api.estimate_cost(workspace, graph_only=True)
    assert set(result['graph_calls'].values()) == {0}
    assert result['graph_estimate_provenance']['mapping_backend'] == 'local'


def test_default_source_pipeline_builds_public_notes_shared_index_and_derived_graph(tmp_path, sample_items, monkeypatch):
    from conftest import FakeReader, FakeZotero
    from auto_zettelkasten.documents import DocumentService
    from auto_zettelkasten.notes import parse_atomic_note

    monkeypatch.setattr(local_mapping, '_LocalMiniLM', Encoder)
    monkeypatch.setattr('auto_zettelkasten.mapping.check_local_mapping_availability', lambda _: None)
    reader = FakeReader()
    report = api.run_map(MapRequest(tmp_path, provider='ollama', model='fake-1'),
                         client=FakeZotero(sample_items), reader=reader, run_id='synthetic-default')
    assert report.cluster_map['status'] == 'derived_local'
    assert (tmp_path / '02_source_memory/indexes/source_catalogue.yml').is_file()
    rows = all_workspace_note_rows(tmp_path)
    assert rows
    for row in rows:
        front, _ = parse_atomic_note((tmp_path / row['note_path']).read_text())
        assert 'source_id' not in front  # The actual public-note contract.
    before = {(tmp_path / row['note_path']): (tmp_path / row['note_path']).read_bytes() for row in rows}
    built = api.build_map(tmp_path)
    assert built.metadata['local_mapping']['stats']['encoded_chunks'] == 0
    service = DocumentService(journal_root=tmp_path / 'synthetic-journal', override_root=tmp_path / 'synthetic-overrides')
    try:
        workspace_id = service.open(tmp_path)['id']
        snapshot = service.index(workspace_id)
        assert snapshot['semantic_map']['status'] == 'current'
        assert snapshot['semantic_map']['communities']
        assert any(row.get('derived') and row['type'] == 'semantic_similarity' for row in snapshot['relations'])
    finally:
        service.close()
    assert all(path.read_bytes() == content for path, content in before.items())


def test_missing_local_setup_blocks_before_any_source_or_zotero_work(tmp_path, sample_items):
    from conftest import FakeReader, FakeZotero
    reader = FakeReader()
    client = FakeZotero(sample_items)
    report = api.run_map(MapRequest(tmp_path, provider='ollama', model='fake-1'), reader=reader,
                         client=client, run_id='synthetic-unconfigured')
    assert report.status == 'blocked'
    assert 'local_mapping_configuration' in report.errors[0]['reason']
    assert reader.calls == 0
    assert not client.inventory_calls
    assert not list((tmp_path / '02_source_memory/notes').glob('*.md'))


def test_community_scales_project_only_current_bound_sources(workspace):
    import json
    from auto_zettelkasten.documents import DocumentService

    built = refresh_local_map(workspace, note_rows=all_workspace_note_rows(workspace),
                              encoder_factory=lambda _: Encoder())
    mapping = built['result']
    members = [row['source_id'] for row in mapping['sources']]
    mapping['communities'] = [{'community_id': 'default-group', 'algorithm': 'leiden',
                               'source_ids': members, 'display_label': 'Competing institutional explanations'}]
    mapping['community_scales'] = [{'scale': 'broad', 'resolution': 0.5, 'communities': [
        {'community_id': 'broad-group', 'algorithm': 'leiden', 'source_ids': members,
         'display_label': 'Institutional research'}]}]
    built['path'].write_text(json.dumps(mapping))
    with_service = DocumentService(journal_root=workspace / 'test-journal')
    try:
        wid = with_service.open(workspace)['id']
        current = with_service.index(wid)['semantic_map']
        assert current['communities'][0]['display_label'] == 'Competing institutional explanations'
        assert current['community_scales'][0]['communities'][0]['display_label'] == 'Institutional research'
        assert len(current['community_scales'][0]['communities'][0]['paths']) == 3
        path = workspace / mapping['sources'][0]['note_path']
        path.write_text(path.read_text() + '\nChanged evidence.\n')
        stale = with_service.index(wid)['semantic_map']
        assert stale['status'] == 'partial'
        assert 'display_label' not in stale['communities'][0]
        assert 'display_label' not in stale['community_scales'][0]['communities'][0]
        assert len(stale['community_scales'][0]['communities'][0]['paths']) == 2
    finally:
        with_service.close()


def test_naming_entrypoint_defaults_to_preview_and_preserves_files(workspace):
    from auto_zettelkasten.mapping import community_name_manifest

    result = refresh_local_map(workspace, note_rows=all_workspace_note_rows(workspace),
                               encoder_factory=lambda _: Encoder())['result']
    group = result['communities'][0]
    class Reader:
        name = 'deepseek'
        model = 'deepseek-v4-flash'
        capabilities = {'capability_identity': 'synthetic-bounded-reader'}
        def community_name_input_budget(self):
            return 8192
        def community_name_output_tokens(self):
            return 128
        def name_community(self, *args):
            pytest.fail('Preview dispatched a provider request')
    before = {path: path.read_bytes() for path in workspace.rglob('*') if path.is_file()}
    preview = community_name_manifest(workspace, group['community_id'], reader=Reader())
    assert preview['status'] == 'deterministic'
    assert preview['provider_calls'] == 0
    assert preview['preview']['paid_dispatch_enabled'] is False
    assert preview['preview']['reserved_cost_usd'] is None
    assert all(path.read_bytes() == raw for path, raw in before.items())
    assert not (workspace / '.auto-zettelkasten/mapping/community-names.sqlite3').exists()
    with pytest.raises(ValueError, match='cloud permission'):
        community_name_manifest(workspace, group['community_id'], reader=Reader(), execute=True)


def test_local_tuning_cli_and_new_entrypoints_are_explicit():
    from auto_zettelkasten.cli import build_parser, _local_mapping_options
    parser = build_parser()
    args = parser.parse_args(['build-map', '--workspace', '/synthetic', '--local-tuning', 'auto'])
    assert _local_mapping_options(args, {})['tuning_mode'] == 'auto'
    args = parser.parse_args(['community-name', '--workspace', '/synthetic', '--community-id', 'community-one'])
    assert not args.execute and not args.allow_cloud and args.approval is None
    args = parser.parse_args(['calibrate-map', '--workspace', '/synthetic', '--reviews', '/private/reviews.json'])
    assert str(args.reviews) == '/private/reviews.json'


def test_incomplete_cached_model_blocks_before_source_charges(tmp_path, sample_items):
    from conftest import FakeReader, FakeZotero
    model = tmp_path / 'incomplete-model'
    model.mkdir()
    for name in ['config.json', 'modules.json', 'tokenizer_config.json']:
        (model / name).write_text('{}')
    reader, client = FakeReader(), FakeZotero(sample_items)
    report = api.run_map(MapRequest(tmp_path, provider='ollama', model='fake-1',
                                    local_mapping={'model_path': str(model)}), reader=reader, client=client,
                         run_id='synthetic-incomplete-model')
    assert report.status == 'blocked'
    assert 'weight file' in report.errors[0]['reason']
    assert reader.calls == 0 and not client.inventory_calls


@pytest.mark.parametrize('backend', ['', False, [], 'automatic'])
def test_public_api_rejects_malformed_backend_before_work(workspace, backend):
    with pytest.raises(ValueError, match='mapping_backend'):
        api.build_map(workspace, mapping_backend=backend)
    with pytest.raises(ValueError, match='mapping_backend'):
        api.estimate_cost(workspace, graph_only=True, mapping_backend=backend)


@pytest.mark.parametrize('decision_status,expected', [('blocked', 'partial'), ('partial', 'partial'),
                                                     ('disabled', 'built'), ('dry_run', 'built')])
def test_relationship_stage_failure_reaches_public_manifest(workspace, monkeypatch, decision_status, expected):
    monkeypatch.setattr(local_mapping, '_LocalMiniLM', Encoder)
    monkeypatch.setattr('auto_zettelkasten.relationship_mapping.run_relationship_mapping',
                        lambda *_a, **_k: {'status': decision_status, 'provider_calls': 0})
    manifest = api.build_map(workspace)
    assert manifest.status == expected
    assert manifest.metadata['literature_map']['status'] == ('partial' if expected == 'partial' else 'completed')


def test_relationship_failure_reaches_pipeline_and_only_applied_links_are_reported(workspace, monkeypatch):
    from auto_zettelkasten.mapping import pipeline_local_map
    monkeypatch.setattr(local_mapping, '_LocalMiniLM', Encoder)
    response = {'status': 'partial', 'application': {'applied': False, 'records': [
        {'status': 'accepted', 'source_id': 'source-a', 'target_source_id': 'source-b'}]}}
    monkeypatch.setattr('auto_zettelkasten.relationship_mapping.run_relationship_mapping', lambda *_a, **_k: response)
    kwargs = {'source_set': {}, 'note_rows': all_workspace_note_rows(workspace), 'request': MapRequest(workspace)}
    preview = pipeline_local_map(workspace, **kwargs)
    assert preview['partial_reason'] == 'relationship_decisions_incomplete'
    assert preview['typed_links']['links'] == []
    response['application']['applied'] = True
    applied = pipeline_local_map(workspace, **kwargs)
    assert len(applied['typed_links']['links']) == 1
