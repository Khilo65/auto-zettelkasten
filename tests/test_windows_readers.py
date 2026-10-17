"""Windows Codex transport contracts without real credentials or subprocesses."""
import base64
import json
import sys
import time
from types import SimpleNamespace

import pytest

from auto_zettelkasten import readers


def test_windows_environment_keeps_os_bootstrap_and_excludes_secrets(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(readers.os, "environ", {
        "SystemRoot": "C:/Windows", "USERPROFILE": str(tmp_path),
        "PATH": "native-tools", "TEMP": "temporary", "PATHEXT": ".EXE;.CMD",
        "OPENAI_API_KEY": "must-not-inherit", "DEEPSEEK_API_KEY": "must-not-inherit",
    })
    env = readers._codex_environment()
    assert env["SystemRoot"] == "C:/Windows"
    assert readers._codex_credential_root(env) == tmp_path / ".codex"
    assert "OPENAI_API_KEY" not in env
    assert "DEEPSEEK_API_KEY" not in env


def test_windows_isolation_redirects_user_state_before_auth_copy(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    secured = []
    monkeypatch.setitem(sys.modules, "auto_zettelkasten.windows_platform", SimpleNamespace(secure_directory=secured.append))
    source = tmp_path / "source"
    source.mkdir()
    claims = base64.urlsafe_b64encode(json.dumps({"exp": time.time() + 7200}).encode()).decode().rstrip("=")
    (source / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"access_token": f"header.{claims}.signature"}}))
    (source / "models_cache.json").write_text("{}")
    root = tmp_path / "call"
    root.mkdir()
    env = readers._isolated_codex_environment({"USERPROFILE": str(source), "HOMEDRIVE": "C:", "HOMEPATH": "/private"}, source, root, 10)
    assert env["USERPROFILE"] == env["HOME"] == str(root / "home")
    assert "HOMEDRIVE" not in env and "HOMEPATH" not in env
    assert env["TMP"] == env["TEMP"]
    for name in ("CODEX_HOME", "APPDATA", "LOCALAPPDATA", "TEMP"):
        assert readers.Path(env[name]).is_relative_to(root)
        assert readers.Path(env[name]) in secured
    assert secured[0] == root
    assert (readers.Path(env["CODEX_HOME"]) / "auth.json").read_bytes() == (source / "auth.json").read_bytes()


def test_windows_isolation_fails_before_copy_if_acl_cannot_be_secured(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    def fail(path):
        raise OSError("ACL failed")
    monkeypatch.setitem(sys.modules, "auto_zettelkasten.windows_platform", SimpleNamespace(secure_directory=fail))
    with pytest.raises(readers.ProviderIsolationFailure, match="could not be secured"):
        readers._isolated_codex_environment({}, tmp_path / "source", tmp_path, 10)
    assert not (tmp_path / "home").exists()


def test_windows_rejects_codex_command_shims(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    shim = tmp_path / "codex.cmd"
    shim.write_text("do not execute")
    with pytest.raises(readers.ProviderError, match="native .exe"):
        readers._codex_executable({"AUTO_ZETTELKASTEN_CODEX": str(shim)})


def test_windows_termination_does_not_use_posix_process_groups(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    calls = []
    process = SimpleNamespace(poll=lambda: None, terminate=lambda: calls.append("terminate"), wait=lambda timeout: calls.append("wait"))
    readers._terminate_codex_process(process)
    assert calls == ["terminate", "wait"]


def test_windows_process_job_cleanup_includes_descendants_after_parent_exits(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    calls = []
    class Job:
        def __init__(self, process):
            calls.append("assign-and-resume")
        def terminate(self):
            calls.append("terminate-tree")
        def close(self):
            calls.append("close-tree")
    monkeypatch.setitem(sys.modules, "auto_zettelkasten.windows_platform", SimpleNamespace(ProcessJob=Job))
    process = SimpleNamespace(poll=lambda: 0)
    readers._attach_codex_process_job(process)
    readers._terminate_codex_process(process)
    readers._close_codex_process_job(process)
    assert calls == ["assign-and-resume", "terminate-tree", "close-tree"]


def test_windows_process_job_failure_closes_transport_before_requests(monkeypatch):
    import io
    monkeypatch.setattr(sys, "platform", "win32")
    def reject(process):
        raise OSError("job assignment failed")
    monkeypatch.setitem(sys.modules, "auto_zettelkasten.windows_platform", SimpleNamespace(ProcessJob=reject))
    process = SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO(), stderr=io.BytesIO())
    with pytest.raises(OSError, match="job assignment failed"):
        readers._attach_codex_process_job(process)
    assert process.stdin.closed and process.stdout.closed and process.stderr.closed
    assert not hasattr(process, "_auto_zettelkasten_job")
