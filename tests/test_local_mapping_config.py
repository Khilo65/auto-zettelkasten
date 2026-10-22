from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib
from types import SimpleNamespace

import pytest

from auto_zettelkasten import cli
from auto_zettelkasten.files import read_yaml, write_yaml
from auto_zettelkasten.models import DEFAULT_CODEX_SOURCE_MODEL, MapRequest
from auto_zettelkasten.workspace import initialize, load_config


def test_new_requests_are_local_and_historical_requests_keep_their_backend(tmp_path):
    request = MapRequest(
        tmp_path, provider="codex", model=DEFAULT_CODEX_SOURCE_MODEL
    )
    assert request.mapping_backend == "local"
    assert request.local_mapping["encoder"] == "minilm"
    assert request.local_mapping["clustering"] == "leiden"
    assert request.literature_model is None
    assert MapRequest.from_dict(request.to_dict()) == request

    legacy = request.to_dict()
    legacy.pop("mapping_backend")
    legacy.pop("local_mapping")
    legacy["literature_model"] = "gpt-5.6-terra"
    restored = MapRequest.from_dict(legacy)
    assert restored.mapping_backend == "llm"
    assert restored.model == DEFAULT_CODEX_SOURCE_MODEL
    assert restored.literature_model == "gpt-5.6-terra"
    with pytest.raises(ValueError, match="requires literature_model"):
        MapRequest(
            tmp_path,
            provider="codex",
            model=DEFAULT_CODEX_SOURCE_MODEL,
            mapping_backend="llm",
        )


def test_request_preserves_explicit_local_options_without_changing_source_role(tmp_path):
    request = MapRequest(
        tmp_path,
        provider="codex",
        model="gpt-5.6-luna",
        local_mapping={
            "encoder": "minilm",
            "clustering": "hdbscan",
            "model_path": str(tmp_path / "cached-model"),
        },
    )
    restored = MapRequest.from_dict(request.to_dict())
    assert restored == request
    assert restored.model == "gpt-5.6-luna"
    assert restored.local_mapping["clustering"] == "hdbscan"
    assert restored.local_mapping["model_path"] == str(tmp_path / "cached-model")


@pytest.mark.parametrize(
    "options",
    [
        {"mapping_backend": "automatic"},
        {"mapping_backend": []},
        {"local_mapping": {"encoder": "qwen"}},
        {"local_mapping": {"clustering": "both"}},
        {"local_mapping": {"silent_paid_fallback": True}},
        {"local_mapping": []},
    ],
)
def test_requests_reject_unsupported_routes_and_malformed_options(tmp_path, options):
    with pytest.raises(ValueError):
        MapRequest(tmp_path, **options)


def test_workspace_defaults_and_legacy_config_upgrade_are_read_only(tmp_path):
    initialize(tmp_path)
    path = tmp_path / "auto-zettelkasten.yml"
    config = load_config(tmp_path)
    assert config["mapping_backend"] == "local"
    assert config["local_mapping"]["encoder"] == "minilm"
    assert config["local_mapping"]["clustering"] == "leiden"

    old = read_yaml(path)
    old.pop("mapping_backend")
    old.pop("local_mapping")
    write_yaml(path, old)
    before = path.read_bytes()
    restored = load_config(tmp_path)
    assert restored["mapping_backend"] == "local"
    assert restored["local_mapping"]["clustering"] == "leiden"
    assert path.read_bytes() == before


@pytest.mark.parametrize("backend", ["local", "llm"])
def test_estimate_accepts_input_only_workspace_without_initializing_it(tmp_path, backend):
    from auto_zettelkasten.api import estimate_cost

    path = tmp_path / "auto-zettelkasten.yml"
    write_yaml(path, {"engine_version": "test", "mapping_backend": backend})
    before = path.read_bytes()
    estimate = estimate_cost(tmp_path, graph_only=True)
    assert estimate["graph_estimate_provenance"]["mapping_backend"] == backend
    assert path.read_bytes() == before
    assert not (tmp_path / "11_state" / "workspace_manifest.yml").exists()


@pytest.mark.parametrize("literature_model", [None, "gpt-5.6-terra"])
def test_doctor_admits_only_source_model_for_local_codex_mapping(tmp_path, monkeypatch, literature_model):
    from auto_zettelkasten import api

    initialize(tmp_path)
    path = tmp_path / "auto-zettelkasten.yml"
    config = read_yaml(path)
    config.update(provider="codex", model=DEFAULT_CODEX_SOURCE_MODEL, literature_model=literature_model)
    write_yaml(path, config)
    calls = []

    def preflight(model, effort, additional_models, **kwargs):
        calls.append((model, additional_models))
        return {"status": "configured", "provider": "codex"}

    monkeypatch.setattr(api, "codex_preflight_status", preflight)
    result = api.doctor(tmp_path, client=SimpleNamespace(status=lambda: {"status": "available"}))
    assert result.checks["provider"]["status"] == "configured"
    assert calls == [(DEFAULT_CODEX_SOURCE_MODEL, ())]


@pytest.mark.parametrize(
    "updates",
    [
        {"mapping_backend": "automatic"},
        {"mapping_backend": []},
        {"local_mapping": "minilm"},
        {"local_mapping": {"clustering": "unknown"}},
    ],
)
def test_invalid_workspace_options_fail_before_execution(tmp_path, updates):
    initialize(tmp_path)
    path = tmp_path / "auto-zettelkasten.yml"
    config = read_yaml(path)
    config.update(updates)
    write_yaml(path, config)
    with pytest.raises(ValueError):
        load_config(tmp_path)


@pytest.mark.parametrize("command", ["map", "sync", "build-map", "estimate"])
def test_cli_forwards_local_config_and_explicit_overrides(
    tmp_path, monkeypatch, capsys, command
):
    initialize(tmp_path)
    path = tmp_path / "auto-zettelkasten.yml"
    config = read_yaml(path)
    config.update(
        mapping_backend="local",
        local_mapping={"encoder": "minilm", "clustering": "hdbscan"},
    )
    write_yaml(path, config)
    calls = []

    def capture(*args, **kwargs):
        if command in {"map", "sync"}:
            request = args[0]
            calls.append((request.mapping_backend, dict(request.local_mapping)))
        else:
            calls.append((kwargs["mapping_backend"], kwargs["local_mapping"]))
        result = {"status": "completed", "artifacts": [], "metadata": {}}
        return result if command in {"sync", "estimate"} else SimpleNamespace(
            to_dict=lambda: result
        )

    target = {
        "map": "run_map", "sync": "sync_zotero",
        "build-map": "build_map", "estimate": "estimate_cost",
    }[command]
    monkeypatch.setattr(cli, target, capture)
    common = [command, "--workspace", str(tmp_path)]
    assert cli.main(common) == 0
    capsys.readouterr()
    assert calls[-1][0] == "local"
    assert calls[-1][1]["clustering"] == "hdbscan"
    model_path = str(tmp_path / "offline-model")
    assert cli.main([
        *common,
        "--mapping-backend", "llm",
        "--local-encoder", "minilm",
        "--local-clustering", "leiden",
        "--embedding-model-path", model_path,
    ]) == 0
    capsys.readouterr()
    assert calls[-1][0] == "llm"
    assert calls[-1][1]["clustering"] == "leiden"
    assert calls[-1][1]["model_path"] == model_path


def test_optional_ml_dependencies_are_not_imported_by_config_or_cli(tmp_path):
    source = Path(__file__).parents[1] / "src"
    script = """
import builtins, json, sys
original = builtins.__import__
blocked = {'torch', 'numpy', 'sentence_transformers', 'igraph', 'leidenalg', 'hdbscan'}
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in blocked:
        raise AssertionError('configuration imported optional dependency: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from auto_zettelkasten.cli import build_parser
from auto_zettelkasten.models import MapRequest
from auto_zettelkasten.workspace import initialize, load_config
initialize(sys.argv[1])
request = MapRequest(sys.argv[1])
config = load_config(sys.argv[1])
args = build_parser().parse_args(['build-map', '--workspace', sys.argv[1]])
print(json.dumps({'backend': request.mapping_backend,
                  'clustering': config['local_mapping']['clustering'],
                  'command': args.command}))
"""
    env = {**os.environ, "PYTHONPATH": str(source)}
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "backend": "local", "clustering": "leiden", "command": "build-map"
    }


def test_hdbscan_installation_is_independent_from_default_mapping_dependencies():
    path = Path(__file__).parents[1] / "pyproject.toml"
    project = tomllib.loads(path.read_text())["project"]
    defaults = project["optional-dependencies"]["local-mapping"]
    assert any(value.startswith("leidenalg") for value in defaults)
    assert not any("hdbscan" in value for value in defaults)
    assert not any("hdbscan" in value for value in project["dependencies"])
    assert any(
        value.startswith("hdbscan")
        for value in project["optional-dependencies"]["local-mapping-hdbscan"]
    )
