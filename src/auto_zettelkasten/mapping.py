"""Workspace integration for derived local maps; never writes source notes."""
from __future__ import annotations

from pathlib import Path
import json
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
    from .local_tools import load_cached_mapping_discovery
    discovery = load_cached_mapping_discovery(root, inputs['projection_rows'])
    paths = [Path(path) for path in inputs.get('input_paths', [])]
    stamps = inputs['input_stamps']
    calibration = None
    calibration_path = cache / 'calibration.json'
    if getattr(settings, 'tuning_mode', 'manual') == 'auto' and calibration_path.exists():
        calibration = _bounded_json(calibration_path)
    ranking_rows = None
    if calibration is not None:
        # Admission must use the same canonical citation/tag/profile signals
        # as paid selection, rather than the richer embedding-text projection.
        from .local_tools import load_local_retrieval
        ranked = load_local_retrieval(root, infer_scope=True)
        selected_ids = {row['source_id'] for row in inputs['projection_rows']}
        ranking_rows = [row for row in ranked['rows'] if row['source_id'] in selected_ids]
        discovery = {'records': [row for row in ranked['discovery_records'] if row['source_id'] in selected_ids],
                     'citation_records': [row for row in ranked['citation_records']
                                          if row['source_id'] in selected_ids and row['target_source_id'] in selected_ids],
                     'status': ranked['status']}

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
        result['discovery_coverage'] = discovery['status']

    result = build_local_map(
        inputs['projection_rows'], cache_dir=cache, options=settings,
        text_sources=inputs['text_sources'], discovery_records=discovery['records'],
        citation_records=discovery.get('citation_records', []),
        ranking_projection_rows=ranking_rows,
        progress=progress, before_commit=before_commit,
        calibration_report=calibration,
        **({'encoder_factory': encoder_factory} if encoder_factory is not None else {}),
    )
    result_path = confined_child(cache, 'result.json')
    return {'result': result, 'path': result_path, 'inputs': inputs}


def _bounded_json(path: Path | str, *, max_bytes: int = 8 * 1024 * 1024) -> dict[str, Any]:
    path = Path(path).expanduser()
    if path.is_symlink() or not path.is_file() or path.stat().st_size > max_bytes:
        raise ValueError('Expected a bounded local JSON object.')
    with path.open('rb') as source:
        raw = source.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError('Local JSON input exceeds its byte allowance.')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('Local JSON input must be an object.')
    return value


def calibrate_local_map(workspace: Path | str, review_input: Path | str) -> dict[str, Any]:
    """Evaluate saved vectors and independently reviewed pairs without encoding."""
    from .files import write_json
    from .local_mapping import LocalMappingOptions
    from .mapping_calibration import calibrate_mapping, load_cached_source_vectors
    from .local_tools import load_local_retrieval
    from .workspace import load_config
    import shutil
    import time

    root = resolve_workspace(workspace)
    config = load_config(root)
    material = _bounded_json(review_input)
    settings = LocalMappingOptions.from_dict(config.get('local_mapping', {}))
    started = time.monotonic()
    cache = confined_child(root, '.auto-zettelkasten', 'mapping')
    def check_resources():
        from .local_mapping import LocalMappingError
        if time.monotonic() - started > settings.max_wall_seconds:
            raise LocalMappingError('Calibration reached its configured wall-time limit.')
        if shutil.disk_usage(cache).free < settings.min_free_disk_bytes:
            raise LocalMappingError('Calibration stopped at its configured free-disk limit.')
    cached = load_cached_source_vectors(cache, options=settings, check_resources=check_resources)
    loaded = load_local_retrieval(root, infer_scope=True)
    report = calibrate_mapping(cached['source_ids'], cached['vectors'], material.get('collections', {}),
                               material.get('reviews', []),
                               options=settings,
                               embedding_identity=cached['embedding_identity'],
                               source_fingerprint=cached['source_fingerprint'],
                               projection_rows=[row for row in loaded['rows'] if row['source_id'] in set(cached['source_ids'])],
                               discovery_records=[row for row in loaded['discovery_records'] if row['source_id'] in set(cached['source_ids'])],
                               citation_records=[row for row in loaded['citation_records']
                                                 if row['source_id'] in set(cached['source_ids']) and row['target_source_id'] in set(cached['source_ids'])],
                               check_resources=check_resources)
    # An incomplete attempt must not replace the last completed evaluation.
    path = cache / ('calibration.json' if report['status'] == 'complete' else 'calibration-last-attempt.json')
    write_json(path, report)
    return {'status': report['status'], 'path': str(path), 'calibration': report, 'provider_calls': 0}


def community_name_manifest(
    workspace: Path | str, community_id: str, *, scale: str = 'default',
    execute: bool = False, approval: Mapping[str, Any] | None = None,
    allow_cloud: bool = False, reader: Any = None,
    input_price_per_million: Any = None, output_price_per_million: Any = None,
    pricing_identity: str = '',
) -> dict[str, Any]:
    """Preview an optional source-worker naming task, with its own approval."""
    from .community_naming import prepare_community_name, generate_community_name
    from .files import write_json
    from .local_mapping import _exclusive_cache
    from .mapping_text import load_mapping_inputs
    from .readers import provider_from_name
    from .relationship_mapping import _section
    from .workspace import load_config, validate_opaque_id

    validate_opaque_id(community_id, field='community_id')
    if scale not in {'broad', 'default', 'fine'} or type(execute) is not bool or type(allow_cloud) is not bool:
        raise ValueError('Invalid community naming options.')
    root = resolve_workspace(workspace)
    config = load_config(root)
    cache = confined_child(root, '.auto-zettelkasten', 'mapping')
    result_path = cache / 'result.json'
    mapping = _bounded_json(result_path, max_bytes=64 * 1024 * 1024)
    groups = mapping.get('communities', [])
    if scale != 'default':
        selected = [row for row in mapping.get('community_scales', []) if row.get('scale') == scale]
        groups = selected[0].get('communities', []) if len(selected) == 1 else []
    found = [row for row in groups if row.get('community_id') == community_id]
    if len(found) != 1:
        raise ValueError('Choose a current community from the saved local map.')
    ids = found[0]['source_ids']
    from .pipeline import all_workspace_note_rows
    inputs = load_mapping_inputs(root, source_ids=ids, note_rows=all_workspace_note_rows(root))
    rows = {row['source_id']: row for row in inputs['projection_rows']}
    saved = {row['source_id']: row for row in mapping.get('sources', [])}
    members = []
    for sid in ids:
        if sid not in rows or sid not in saved:
            raise ValueError('Community membership is unavailable; refresh the local map.')
        row, prior = rows[sid], saved[sid]
        if any(row.get(key, '') != prior.get(key, '') for key in ('projection_hash', 'metadata_file_sha256', 'profile_file_sha256')):
            raise ValueError('Community evidence changed; refresh the local map before naming.')
        text = inputs['text_sources'].get(sid, {}).get('text', '')
        section = _section(text, 'Thesis')
        members.append({'source_id': sid, 'title': row.get('title') or row.get('note_id') or sid,
                        'thesis': section[0] if section else ''})
    provider, model = str(config['provider']), str(config['model'])
    if execute and provider != 'ollama' and not allow_cloud:
        raise ValueError('Community naming requires explicit cloud permission as well as its own approval.')
    source_reader = reader or provider_from_name(provider, model, allow_cloud=allow_cloud,
                                                reasoning_effort=config.get('reasoning_effort'))
    preview = prepare_community_name(source_reader, members, source_provider=provider, source_model=model,
                                     input_price_per_million=input_price_per_million,
                                     output_price_per_million=output_price_per_million,
                                     pricing_identity=pricing_identity)
    named = generate_community_name(source_reader, preview, store_path=cache / 'community-names.sqlite3',
                                    enabled=execute, approval=approval)
    if execute:
        with _exclusive_cache(cache):
            current = _bounded_json(result_path, max_bytes=64 * 1024 * 1024)
            if current != mapping:
                raise ValueError('Local map changed while naming; cached title retained but not applied.')
            # Recheck the exact underlying inputs before publishing a derived label.
            for filename, stamp in inputs['input_stamps'].items():
                path = Path(filename)
                info = path.stat()
                if path.is_symlink() or [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns] != stamp:
                    raise ValueError('Community evidence changed while naming; cached title retained but not applied.')
            found[0]['display_label'] = named['title']
            found[0]['naming_identity'] = preview['request_digest']
            write_json(result_path, mapping)
    return {'status': named['status'], 'community_id': community_id, 'scale': scale,
            'preview': preview, 'naming': named, 'provider_calls': int(named['status'] == 'generated')}


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
                       run_id: str = '', progress=None, encoder_factory=None,
                       relationship_decisions=None) -> ArtifactManifest:
    root = resolve_workspace(workspace)
    built = refresh_local_map(root, source_ids=source_ids, note_rows=note_rows, options=options,
                              progress=progress, encoder_factory=encoder_factory)
    result = built['result']
    stats = result.get('stats', {})
    partial = any(row['reason'] != 'density_noise_or_isolate' for row in _unmapped_sources(built))
    from .relationship_mapping import run_relationship_mapping
    decisions = run_relationship_mapping(root, result.get('candidate_neighbors', result.get('neighbors', [])),
                                         options=relationship_decisions, source_ids=source_ids,
                                         preferred_semantic_neighbors=result.get('neighbors', []))
    partial = partial or decisions.get('status') in {'partial', 'blocked'}
    return ArtifactManifest(
        status='partial' if partial else 'built', workspace=root, run_id=run_id,
        created_at=now_iso(), artifacts=artifact_rows(root, [built['path']]),
        metadata={'mapping_backend': 'local', 'local_mapping': result,
                  'relationship_decisions': decisions,
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
    from .relationship_mapping import run_relationship_mapping
    decisions = run_relationship_mapping(workspace, result.get('candidate_neighbors', result.get('neighbors', [])),
                                         options=request.relationship_decisions, source_ids=source_ids,
                                         preferred_semantic_neighbors=result.get('neighbors', []))
    sources = result.get('sources', [])
    unmapped = _unmapped_sources(built)
    partial = any(row['reason'] != 'density_noise_or_isolate' for row in unmapped)
    decision_partial = decisions.get('status') in {'partial', 'blocked'}
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
        'typed_links': {'links': [row for row in decisions.get('application', {}).get('records', [])
                                  if decisions.get('application', {}).get('applied') and row.get('status') == 'accepted']},
        'profiles': built['inputs']['projection_rows'],
        'relationship_decisions': decisions,
        'profile_result': {'valid_count': len(sources), 'excluded_count': 0, 'provider_calls': 0,
                           'checkpoint_hits': 0, 'failure_count': 0},
        'migration': {}, 'paths': [built['path']],
        'partial_reason': ';'.join(reason for reason, present in (
            ('local_mapping_missing_or_ineligible_content', partial),
            ('relationship_decisions_incomplete', decision_partial)) if present),
    }
