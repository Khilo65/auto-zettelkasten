"""Workspace integration for derived local maps; never writes source notes."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from .files import now_iso
from .models import ArtifactManifest
from .workspace import artifact_rows, confined_child, resolve_workspace


def check_local_mapping_availability(options=None):
    """Reject missing local setup before an ordinary run can charge for source work."""
    import importlib.util
    from .local_mapping import LocalMappingError, LocalMappingOptions
    settings = LocalMappingOptions.from_dict(options)
    names = ['numpy', 'torch', 'transformers', 'sentence_transformers']
    names.extend(['hdbscan'] if settings.clustering == 'hdbscan' else ['igraph', 'leidenalg'])
    if any(importlib.util.find_spec(name) is None for name in names):
        raise LocalMappingError('Local mapping dependencies are unavailable. Install auto-zettelkasten[local-mapping]' +
                                (' and auto-zettelkasten[local-mapping-hdbscan]' if settings.clustering == 'hdbscan' else '') +
                                '; no source or provider work was started.')
    if not settings.model_path:
        raise LocalMappingError('Set local_mapping.model_path to an existing cached all-MiniLM-L6-v2 directory before mapping. No model download or provider fallback is performed.')
    path = Path(settings.model_path).expanduser()
    if not path.is_dir() or not all((path / name).is_file() for name in ['config.json', 'modules.json', 'tokenizer_config.json']):
        raise LocalMappingError('The configured local MiniLM directory is unavailable or incomplete; no source work was started.')
    assets = []
    for entry in path.rglob('*'):
        if entry.is_file():
            assets.append(entry)
        if len(assets) > 256:
            raise LocalMappingError('The configured model exceeds the supported local asset inventory; no source work was started.')
    if not any(entry.suffix in {'.safetensors', '.bin'} for entry in assets):
        raise LocalMappingError('The configured MiniLM model has no cached weight file; no source work was started.')
    if not any((path / name).is_file() for name in ['tokenizer.json', 'vocab.txt']):
        raise LocalMappingError('The configured MiniLM tokenizer assets are incomplete; no source work was started.')
    import json
    try:
        modules = json.loads((path / 'modules.json').read_text())
        pools = [entry for entry in modules if str(entry.get('type', '')).endswith('.Pooling')]
        pool_path = Path(pools[0]['path']) if len(pools) == 1 else None
        if pool_path is None or pool_path.is_absolute() or '..' in pool_path.parts or not (path / pool_path / 'config.json').is_file():
            raise ValueError('missing mean-pooling configuration')
        config = json.loads((path / 'config.json').read_text())
        pool = json.loads((path / pool_path / 'config.json').read_text())
        if config.get('hidden_size') != 384 or config.get('num_hidden_layers') != 6 or pool.get('pooling_mode_mean_tokens') is not True or any(value for key, value in pool.items() if key.startswith('pooling_mode_') and key != 'pooling_mode_mean_tokens'):
            raise ValueError('unsupported MiniLM architecture or pooling')
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
        raise LocalMappingError('The cached MiniLM configuration or pooling assets are incomplete or unsupported; no source work was started.') from exc



def refresh_local_map(
    workspace: Path | str, *, note_rows: Sequence[Mapping[str, Any]] | None = None,
    source_ids: Sequence[str] | None = None, options: Mapping[str, Any] | None = None,
    progress: Any = None, encoder_factory: Any = None,
) -> dict[str, Any]:
    from .local_mapping import LocalMappingOptions, build_local_map
    from .mapping_text import load_mapping_inputs

    root = resolve_workspace(workspace)
    cache = confined_child(root, '.auto-zettelkasten', 'mapping')
    settings = LocalMappingOptions.from_dict(options)
    inputs = load_mapping_inputs(root, note_rows=note_rows, source_ids=source_ids)
    paths = [Path(path) for path in inputs.get('input_paths', [])]
    stamps = inputs['input_stamps']

    def current_stamp(path):
        resolved = path.resolve()
        if path.is_symlink() or not resolved.is_relative_to(root) or not path.is_file():
            return None
        value = path.stat()
        return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns]

    def before_commit(result):
        if any(current_stamp(path) != stamps[str(path)] for path in paths):
            raise RuntimeError('Mapping inputs changed during the job; checkpoints are preserved, refresh the map again.')
        result['input_coverage'] = inputs['coverage']
        result['derived'] = True
        result['accepted_relationships'] = False
        result['created_at'] = now_iso()

    result = build_local_map(
        inputs['projection_rows'], cache_dir=cache, options=settings,
        text_sources=inputs['text_sources'], progress=progress, before_commit=before_commit,
        **({'encoder_factory': encoder_factory} if encoder_factory is not None else {}),
    )
    result_path = confined_child(cache, 'result.json')
    return {'result': result, 'path': result_path, 'inputs': inputs}


def _unmapped_sources(built):
    result = built['result']
    excluded = [{'source_id': row['source_id'], 'reason': row.get('coverage', {}).get('status', 'unavailable')}
                for row in result.get('sources', []) if not row.get('chunk_ids')]
    excluded.extend({'source_id': sid, 'reason': 'missing_input_text'}
                    for sid in built['inputs']['coverage'].get('missing_source_ids', []))
    member_ids = {sid for community in result.get('communities', []) for sid in community.get('source_ids', [])}
    excluded.extend({'source_id': row['source_id'], 'reason': 'density_noise_or_isolate'}
                    for row in result.get('sources', []) if row.get('chunk_ids') and row['source_id'] not in member_ids)
    return excluded


def local_map_manifest(workspace: Path | str, *, source_ids=None, note_rows=None, options=None,
                       run_id: str = '', progress=None, encoder_factory=None) -> ArtifactManifest:
    root = resolve_workspace(workspace)
    built = refresh_local_map(root, source_ids=source_ids, note_rows=note_rows, options=options,
                              progress=progress, encoder_factory=encoder_factory)
    result = built['result']
    stats = result.get('stats', {})
    partial = any(row['reason'] != 'density_noise_or_isolate' for row in _unmapped_sources(built))
    return ArtifactManifest(
        status='partial' if partial else 'built', workspace=root, run_id=run_id,
        created_at=now_iso(), artifacts=artifact_rows(root, [built['path']]),
        metadata={'mapping_backend': 'local', 'local_mapping': result,
                  'literature_map': {'status': 'partial' if partial else 'completed',
                                     'cluster_count': len(result.get('communities', [])),
                                     'semantic_neighbor_count': len(result.get('neighbors', [])),
                                     'source_count': len(result.get('sources', [])),
                                     'provider_calls': 0, 'stats': stats,
                                     'coverage': built['inputs']['coverage']},
                  'source_set': {'source_ids': [row['source_id'] for row in result.get('sources', [])]}}
    )


def pipeline_local_map(workspace, *, source_set, note_rows, request, progress=None):
    """Adapt derived output to the existing run-report contract without note projection."""
    if progress is not None:
        progress.set_stage('local_mapping')
    source_ids = source_set.get('source_ids')
    def update(event):
        if progress is not None:
            progress.update_literature(local_mapping_phase=event['phase'],
                                       local_mapping_completed_chunks=event.get('completed_chunks', 0),
                                       local_mapping_total_chunks=event.get('total_chunks', event.get('chunks', 0)))

    built = refresh_local_map(workspace, source_ids=source_ids, note_rows=note_rows,
                              options=request.local_mapping, progress=update)
    result = built['result']
    sources = result.get('sources', [])
    unmapped = _unmapped_sources(built)
    partial = any(row['reason'] != 'density_noise_or_isolate' for row in unmapped)
    communities = [{**row, 'cluster_id': row['community_id'], 'derived': True}
                   for row in result.get('communities', [])]
    if progress is not None:
        progress.update_literature(cluster_count=len(communities), synthesis_call_count=0,
                                   profile_provider_calls=0, semantic_neighbor_count=len(result.get('neighbors', [])))
    return {
        'source_set': dict(source_set),
        'cluster_map': {'status': 'derived_local', 'clusters': communities,
                        'unclustered_sources': unmapped, 'semantic_neighbors': result.get('neighbors', []),
                        'coverage': built['inputs']['coverage'], 'derived': True},
        'gap_map': {'status': 'not_adjudicated', 'gap_candidates': [], 'novelty_claimed': False},
        'literature_packet': {'status': 'derived_local', 'synthesis_call_count': 0,
                              'synthesis_new_call_count': 0, 'synthesis_failure_count': 0,
                              'local_mapping': result},
        'typed_links': {'links': []}, 'profiles': built['inputs']['projection_rows'],
        'profile_result': {'valid_count': len(sources), 'excluded_count': 0, 'provider_calls': 0,
                           'checkpoint_hits': 0, 'failure_count': 0},
        'migration': {}, 'paths': [built['path']],
        'partial_reason': 'local_mapping_missing_or_ineligible_content' if partial else '',
    }
