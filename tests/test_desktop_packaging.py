"""Desktop build contracts; runtime smoke uses synthetic text and no model weights."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tomllib

import pytest


ROOT = Path(__file__).parents[1]


def load_tool(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("platform,renderer", [("darwin", "cocoa"), ("win32", "edgechromium")])
def test_build_command_keeps_local_mapping_runtime_and_platform_renderer(monkeypatch, platform, renderer):
    build = load_tool("build_desktop")
    monkeypatch.setattr(build.sys, "platform", platform)
    monkeypatch.setattr(build, "check_dependencies", lambda: None)
    monkeypatch.setattr(build.importlib.util, "find_spec", lambda name: None if name == "hdbscan" else SimpleNamespace())
    command = build.build_command()
    # Parse the actual command with PyInstaller, including all repeated options.
    pyinstaller = pytest.importorskip("PyInstaller.__main__")
    options = pyinstaller.generate_parser().parse_args(command[3:])
    assert f"webview.platforms.{renderer}" in options.hiddenimports
    excluded = set(options.excludes)
    assert not excluded.intersection({"numpy", "scipy", "torch", "sklearn", "igraph", "leidenalg"})
    assert "hdbscan" not in options.collect_all
    assert "transformers.models.bert" in options.collect_submodules
    assert "transformers" not in options.collect_all + options.collect_submodules
    assert "sentence_transformers.models.Transformer" in options.hiddenimports
    assert {"igraph", "leidenalg", "tokenizers", "safetensors"} <= set(options.collect_all)
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    # Every direct optional runtime must retain metadata, including transitive
    # NumPy/Torch versions used to identify persisted mapping results.
    dependencies = {canonicalize_name(Requirement(item).name) for item in project["optional-dependencies"]["local-mapping"]}
    roots = {canonicalize_name(item) for item in options.recursive_copy_metadata}
    assert dependencies <= roots
    assert options.filenames == [str(ROOT / "tools" / "desktop_entry.py")]
    assert options.datas == [(str(ROOT / "src" / "auto_zettelkasten" / "web"), "auto_zettelkasten/web")]


def test_build_requires_mapping_dependencies_before_invoking_pyinstaller(monkeypatch):
    build = load_tool("build_desktop")
    monkeypatch.setattr(build.sys, "platform", "darwin")
    monkeypatch.setattr(build.importlib.util, "find_spec", lambda name: None if name == "torch" else SimpleNamespace())
    monkeypatch.setattr(build.importlib.metadata, "version", lambda name: "1.0")
    with pytest.raises(SystemExit, match=r"torch.*desktop,desktop-build,local-mapping"):
        build.build_command()


def test_build_rejects_missing_runtime_metadata(monkeypatch):
    build = load_tool("build_desktop")
    monkeypatch.setattr(build.sys, "platform", "darwin")
    monkeypatch.setattr(build.importlib.util, "find_spec", lambda name: SimpleNamespace())

    def version(name):
        if name == "sentence-transformers":
            raise build.importlib.metadata.PackageNotFoundError(name)
        return "1.0"

    monkeypatch.setattr(build.importlib.metadata, "version", version)
    with pytest.raises(SystemExit, match="sentence-transformers"):
        build.build_command()


def test_default_dependency_check_does_not_require_hdbscan(monkeypatch):
    build = load_tool("build_desktop")
    monkeypatch.setattr(build.importlib.util, "find_spec", lambda name: None if name == "hdbscan" else SimpleNamespace())
    monkeypatch.setattr(build.importlib.metadata, "version", lambda name: "1.0")
    build.check_dependencies()


def test_hdbscan_is_collected_only_when_installed(monkeypatch):
    build = load_tool("build_desktop")
    monkeypatch.setattr(build.sys, "platform", "darwin")
    monkeypatch.setattr(build, "check_dependencies", lambda: None)
    monkeypatch.setattr(build.importlib.util, "find_spec", lambda name: SimpleNamespace())
    pyinstaller = pytest.importorskip("PyInstaller.__main__")
    options = pyinstaller.generate_parser().parse_args(build.build_command()[3:])
    assert "hdbscan" in options.collect_all
    assert "hdbscan" in options.copy_metadata


def test_normal_entry_does_not_load_ml_and_preserves_desktop_arguments(monkeypatch):
    import builtins
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        assert name.split(".")[0] not in {"numpy", "torch", "transformers", "sentence_transformers", "igraph", "leidenalg"}
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    entry = load_tool("desktop_entry")
    calls = []
    monkeypatch.setattr(entry, "desktop_main", lambda args: calls.append(args) or 7)
    assert entry.main([]) == 7
    assert entry.main(["--smoke-test"]) == 7
    assert calls == [[], ["--smoke-test"]]


def test_mapping_smoke_reports_failure_with_nonzero_exit(tmp_path, monkeypatch):
    entry = load_tool("desktop_entry")
    report = tmp_path / "runtime.json"
    monkeypatch.setattr(entry, "local_mapping_smoke_test", lambda: (_ for _ in ()).throw(ImportError("missing native library")))
    assert entry.main(["--local-mapping-smoke-test", "--smoke-report", str(report)]) == 1
    assert json.loads(report.read_text()) == {"status": "failed", "error": "ImportError: missing native library"}
    monkeypatch.setattr(entry, "local_mapping_smoke_test", lambda: {"status": "passed", "checks": ["synthetic"]})
    assert entry.main(["--local-mapping-smoke-test", "--smoke-report", str(report)]) == 0
    assert json.loads(report.read_text())["status"] == "passed"


def test_local_runtime_smoke_without_network_or_cached_weights(tmp_path, monkeypatch):
    for module in ("numpy", "torch", "transformers", "sentence_transformers", "igraph", "leidenalg"):
        if importlib.util.find_spec(module) is None:
            pytest.skip("Install local-mapping to exercise the real native runtime.")
    import socket

    def no_network(*args, **kwargs):
        pytest.fail("Local runtime smoke attempted a network connection.")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setenv("HF_HOME", str(tmp_path / "empty-huggingface-home"))
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    entry = load_tool("desktop_entry")
    result = entry.local_mapping_smoke_test()
    assert result["status"] == "passed"
    assert {"cpu_tensor", "leiden_native", "local_tokenizer", "mapping_metadata"} <= set(result["checks"])
