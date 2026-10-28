import importlib.util
import ctypes
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def tool():
    path = Path(__file__).resolve().parents[1] / "tools" / "prepare_comparison_queries.py"
    spec = importlib.util.spec_from_file_location("query_prep", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_warning_pressure_blocks_before_tokenizer_or_encoder(tmp_path, monkeypatch):
    module = tool()
    monkeypatch.setattr(module, "pressure_level", lambda: 2)
    monkeypatch.setattr(module, "swap_used_bytes", lambda: 0)
    def forbidden(*args):
        raise AssertionError("encoder must not be constructed")
    monkeypatch.setattr(module, "_LocalMiniLM", forbidden)
    output = tmp_path / "paused"
    assert module.main(["--tasks", "not-read", "--vector-manifest", "not-read", "--model-path", "not-read", "--output", str(output)]) == 0
    row = json.loads((output / "status.json").read_text())
    assert row["status"] == "paused_resources"
    assert row["encoder_loads"] == row["provider_calls"] == 0


def test_unknown_pressure_and_swap_growth_fail_closed(tmp_path, monkeypatch):
    module = tool()
    monkeypatch.setattr(module, "pressure_level", lambda: None)
    with pytest.raises(RuntimeError, match="not verified normal"):
        module.resource_guard(tmp_path, 0)
    monkeypatch.setattr(module, "pressure_level", lambda: 1)
    monkeypatch.setattr(module, "swap_used_bytes", lambda: 300 * 1024 ** 2)
    with pytest.raises(RuntimeError, match="swap grew"):
        module.resource_guard(tmp_path, 0)


def test_long_queries_are_rejected_before_encoding(tmp_path, monkeypatch):
    module = tool()
    monkeypatch.setattr(module, "resource_guard", lambda *args: None)
    assets = {"model_name": "synthetic-model", "model_sha256": "a" * 64}
    manifest = {"model_assets": assets, "libraries": {"runtime": "synthetic"}, "normalization": "l2", "dimension": 2}
    inputs = tmp_path / "manifest.json"
    inputs.write_text(json.dumps(manifest))
    tasks = tmp_path / "tasks.json"
    tasks.write_text(json.dumps([{"id": "q", "query": "a long question"}]))
    fake = SimpleNamespace(identity={**assets, "library_versions": manifest["libraries"]},
                           dimension=2, max_seq_length=256,
                           tokenizer=SimpleNamespace(encode=lambda *a, **k: list(range(257))))
    monkeypatch.setattr(module, "_LocalMiniLM", lambda *args: fake)
    with pytest.raises(ValueError, match="token window"):
        module.main(["--tasks", str(tasks), "--vector-manifest", str(inputs), "--model-path", "unused", "--output", str(tmp_path / "out")])


def test_native_pressure_readings_are_fresh_without_subprocess_or_ttl(monkeypatch):
    module = tool()
    monkeypatch.setattr(module.sys, "platform", "darwin")
    values = iter([1, 2])
    calls = []
    def native(name, output, size, new_value, new_size):
        calls.append(name)
        assert new_value is None and new_size == 0
        assert size._obj.value == ctypes.sizeof(ctypes.c_uint32)
        output._obj.value = next(values)
        return 0
    monkeypatch.setattr(module, "_sysctl_function", lambda: native)
    def forbidden(*args, **kwargs):
        raise AssertionError("successful native readings must not spawn sysctl")
    monkeypatch.setattr(module.subprocess, "run", forbidden)
    assert module.pressure_level() == 1
    assert module.pressure_level() == 2
    assert calls == [b"kern.memorystatus_vm_pressure_level"] * 2


def test_native_swap_returns_exact_bytes(monkeypatch):
    module = tool()
    monkeypatch.setattr(module.sys, "platform", "darwin")
    amounts = iter([300 * 1024 ** 2, 301 * 1024 ** 2])
    def native(name, output, size, new_value, new_size):
        assert name == b"vm.swapusage"
        assert size._obj.value == ctypes.sizeof(module._SwapUsage) == 32
        value = output._obj
        value.total = 1024 ** 3
        value.used = next(amounts)
        value.available = value.total - value.used
        value.page_size = 16384
        value.encrypted = 1
        return 0
    monkeypatch.setattr(module, "_sysctl_function", lambda: native)
    assert module.swap_used_bytes() == 300 * 1024 ** 2
    assert module.swap_used_bytes() == 301 * 1024 ** 2


@pytest.mark.parametrize("failure", ["errno", "size", "unavailable"])
def test_bad_native_reading_uses_existing_cli_fallback(monkeypatch, failure):
    module = tool()
    monkeypatch.setattr(module.sys, "platform", "darwin")
    def native(name, output, size, new_value, new_size):
        if failure == "unavailable":
            raise AttributeError("sysctlbyname unavailable")
        if failure == "size":
            size._obj.value = 1
            return 0
        return -1
    monkeypatch.setattr(module, "_sysctl_function", lambda: native)
    calls = []
    def cli(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout="1" if command[-1].endswith("level") else "total = 20.00M used = 10.00M free = 10.00M")
    monkeypatch.setattr(module.subprocess, "run", cli)
    assert module.pressure_level() == 1
    assert module.swap_used_bytes() == 10 * 1024 ** 2
    assert len(calls) == 2


def test_invalid_native_swap_layout_falls_back(monkeypatch):
    module = tool()
    monkeypatch.setattr(module.sys, "platform", "darwin")
    def invalid(name, value):
        value.total = 1
        value.used = 2
        return value
    monkeypatch.setattr(module, "_native_sysctl", invalid)
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout="used = 7.50M"))
    assert module.swap_used_bytes() == int(7.5 * 1024 ** 2)


def test_missing_native_and_cli_readings_remain_unknown(monkeypatch):
    module = tool()
    monkeypatch.setattr(module.sys, "platform", "darwin")
    def unavailable(*args, **kwargs):
        raise OSError("unavailable")
    monkeypatch.setattr(module, "_native_sysctl", unavailable)
    monkeypatch.setattr(module.subprocess, "run", unavailable)
    assert module.pressure_level() is None
    assert module.swap_used_bytes() is None


def test_non_darwin_remains_unknown_without_attempting_native_or_cli(monkeypatch):
    module = tool()
    monkeypatch.setattr(module.sys, "platform", "linux")
    def forbidden(*args, **kwargs):
        raise AssertionError("Darwin resource APIs must not run on another platform")
    monkeypatch.setattr(module, "_native_sysctl", forbidden)
    monkeypatch.setattr(module.subprocess, "run", forbidden)
    assert module.pressure_level() is None
    assert module.swap_used_bytes() is None


@pytest.mark.parametrize("disk_free,rss,reason", [
    (2 * 1024 ** 3 - 1, 0, "free disk"),
    (3 * 1024 ** 3, 2 * 1024 ** 3 + 1, "RSS guard"),
])
def test_original_disk_and_rss_limits_are_unchanged(tmp_path, monkeypatch, disk_free, rss, reason):
    module = tool()
    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(module, "pressure_level", lambda: 1)
    monkeypatch.setattr(module, "swap_used_bytes", lambda: 0)
    monkeypatch.setattr(module.shutil, "disk_usage", lambda path: SimpleNamespace(free=disk_free))
    monkeypatch.setattr(module.resource, "getrusage", lambda *args: SimpleNamespace(ru_maxrss=rss))
    with pytest.raises(RuntimeError, match=reason):
        module.resource_guard(tmp_path, 0)
