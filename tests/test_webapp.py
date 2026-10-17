from __future__ import annotations

import http.client
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_zettelkasten import webapp


class Client:
    def __init__(self):
        self.items = [{"key": "DOC1", "data": {"title": "A paper"}}]

    def collections(self):
        return [{"key": "ABC12345", "data": {"name": "My research"}}]

    def inventory(self, scope, collection_key=None):
        return self.items


def test_collection_paths_distinguish_nested_collections(tmp_path):
    client = Client()
    client.collections = lambda: [
        {"key": "PARENT", "data": {"name": "Thesis"}},
        {"key": "CHILD", "data": {"name": "Reading", "parentCollection": "PARENT"}},
        {"key": "OTHER", "data": {"name": "Reading"}},
    ]
    app = webapp.LocalApp(preferences_path=tmp_path / "prefs.json", client=client)
    rows = app.handle("GET", "/api/collections")["collections"]
    assert rows[1]["path"] == "Thesis / Reading"
    assert rows[2]["path"] == "Reading"


@pytest.fixture
def app(tmp_path, monkeypatch):
    app = webapp.LocalApp(preferences_path=tmp_path / "preferences.json", client=Client())
    monkeypatch.setattr(webapp.api, "estimate_cost", lambda *a, **kw: {"inventory_count": len(kw["zotero_client"].inventory("collection", "ABC12345")), "cost": {"expected_usd": 0.5}})
    app.handle("POST", "/api/connect", {"method": "deepseek", "api_key": "secret-test-key"})
    return app


def estimate(app, tmp_path, **kwargs):
    return app.handle("POST", "/api/estimate", {"collection_key": "ABC12345", "output_folder": str(tmp_path / "output"), **kwargs})


def wait_done(app):
    for _ in range(100):
        if app.run["status"] != "running":
            return
        time.sleep(0.01)
    pytest.fail("Mapping worker did not finish")


def test_estimate_does_not_map_or_write_destination(app, tmp_path, monkeypatch):
    monkeypatch.setattr(webapp.api, "run_map", lambda *a, **kw: pytest.fail("Mapping before approval"))
    result = estimate(app, tmp_path)
    assert result["workspace"].endswith("my-research-abc12345")
    assert not (tmp_path / "output").exists()
    assert "secret-test-key" not in json.dumps(result)
    app.handle("POST", "/api/cancel", {"estimate_id": result["estimate_id"]})
    with pytest.raises(webapp.RequestError):
        app.handle("POST", "/api/approve", {"estimate_id": result["estimate_id"], "allow_cloud": True})


def test_exact_approval_one_use_and_frozen_membership(app, tmp_path, monkeypatch):
    result = estimate(app, tmp_path)
    app.client.items.append({"key": "NEW"})
    calls = []

    def mapper(request, *, client, run_id):
        calls.append((request, client.inventory("collection", "ABC12345"), run_id))
        return SimpleNamespace(to_dict=lambda: {"status": "complete", "errors": []})

    monkeypatch.setattr(webapp.api, "run_map", mapper)
    with pytest.raises(webapp.RequestError):
        app.handle("POST", "/api/approve", {"estimate_id": result["estimate_id"]})
    app.handle("POST", "/api/approve", {"estimate_id": result["estimate_id"], "allow_cloud": True})
    wait_done(app)
    assert len(calls) == 1 and len(calls[0][1]) == 1
    assert calls[0][0].allow_cloud
    assert calls[0][0].literature_policy.cluster_generation_enabled is False
    assert app.run["status"] == "completed"
    with pytest.raises(webapp.RequestError):
        app.handle("POST", "/api/approve", {"estimate_id": result["estimate_id"], "allow_cloud": True})


def test_changed_auth_invalidates_estimate(app, tmp_path):
    result = estimate(app, tmp_path)
    app.handle("POST", "/api/connect", {"method": "deepseek", "api_key": "changed-key"})
    with pytest.raises(webapp.RequestError):
        app.handle("POST", "/api/approve", {"estimate_id": result["estimate_id"], "allow_cloud": True})


def test_remember_and_forget_vault(app, tmp_path):
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    app.handle("POST", "/api/preferences", {"vault": str(vault), "remember_vault": True})
    reopened = webapp.LocalApp(preferences_path=app.preferences_path, client=Client())
    assert reopened.preferences == {"vault": str(vault), "remember_vault": True}
    assert "secret-test-key" not in app.preferences_path.read_text()
    app.handle("POST", "/api/preferences", {"vault": "", "remember_vault": False})
    assert webapp.LocalApp(preferences_path=app.preferences_path, client=Client()).preferences["vault"] == ""


def test_codex_executable_is_remembered_and_survives_vault_changes(app, tmp_path, monkeypatch):
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"test executable")
    monkeypatch.setattr(webapp, "_codex_environment", lambda: {})
    monkeypatch.setattr(webapp, "_codex_executable", lambda env: Path(env["AUTO_ZETTELKASTEN_CODEX"]))
    monkeypatch.setattr(webapp, "codex_preflight_status", lambda *args: {})
    monkeypatch.setenv("AUTO_ZETTELKASTEN_CODEX", "previous-test-value")
    app.handle("POST", "/api/connect", {"method": "codex", "codex_executable": str(executable)})
    app.handle("POST", "/api/preferences", {"remember_vault": False})
    reopened = webapp.LocalApp(preferences_path=app.preferences_path, client=Client())
    assert reopened.codex_executable == str(executable)
    app.handle("POST", "/api/connect", {"method": "codex", "codex_executable": ""})
    assert webapp.LocalApp(preferences_path=app.preferences_path, client=Client()).codex_executable == ""


def test_login_timeout_kills_and_reaps_before_unavailable(app, monkeypatch):
    calls = []
    class Process:
        def wait(self, timeout):
            calls.append(("wait", timeout))
            if len(calls) < 4:
                raise webapp.subprocess.TimeoutExpired("codex", timeout)
            return -1

        def terminate(self):
            calls.append(("terminate",))

        def kill(self):
            calls.append(("kill",))

    monkeypatch.setattr(webapp, "_codex_environment", lambda: {})
    monkeypatch.setattr(webapp, "_codex_executable", lambda env: Path("/fake/codex"))
    monkeypatch.setattr(webapp, "_attach_codex_process_job", lambda process: None)
    monkeypatch.setattr(webapp.subprocess, "Popen", lambda *args, **kwargs: Process())
    app._login()
    assert calls == [("wait", 300), ("terminate",), ("wait", 10), ("kill",), ("wait", 10)]
    assert app.auth["status"] == "unavailable"


def test_obsidian_exact_destination_and_collision_guard(app, tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    result = estimate(app, tmp_path, destination="obsidian", vault=str(vault))
    export = Path(result["obsidian_path"])
    export.mkdir(parents=True)
    (export / "human-note.md").write_text("preserve")
    with pytest.raises(webapp.RequestError, match="already exists"):
        app.handle("POST", "/api/approve", {"estimate_id": result["estimate_id"], "allow_cloud": True})
    assert (export / "human-note.md").read_text() == "preserve"
    second = estimate(app, tmp_path, destination="obsidian", vault=str(vault))
    assert second["obsidian_path"] != result["obsidian_path"]
    calls = []
    monkeypatch.setattr(webapp.api, "run_map", lambda *a, **kw: SimpleNamespace(to_dict=lambda: {"status": "complete"}))
    monkeypatch.setattr(webapp.api, "export_to_obsidian", lambda workspace, target, **kw: calls.append((target, kw)) or SimpleNamespace(to_dict=lambda: {"status": "exported"}))
    app.handle("POST", "/api/approve", {"estimate_id": second["estimate_id"], "allow_cloud": True})
    wait_done(app)
    assert calls[0][0] == vault
    assert calls[0][1]["project_folder"] == Path(second["obsidian_path"]).name


def test_http_session_origin_and_request_validation(app):
    server = webapp.LocalServer(("127.0.0.1", 0), app=app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
    try:
        connection.request("GET", "/api/state")
        assert connection.getresponse().status == 403
        connection.close()
        headers = {"X-Session-Token": app.token}
        connection.request("GET", "/api/state", headers=headers)
        response = connection.getresponse()
        state = json.loads(response.read())
        assert response.status == 200
        assert state["auth"]["ready"]
        assert "secret-test-key" not in json.dumps(state)
        connection.request("GET", "/api/state", headers={**headers, "Origin": "https://evil.example"})
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        connection.request("GET", "/api/state", headers={**headers, "Host": "evil.example"})
        response = connection.getresponse()
        assert response.status == 403
        response.read()
        connection.request("POST", "/api/connect", body="[]", headers={**headers, "Content-Type": "application/json"})
        response = connection.getresponse()
        assert response.status == 400
        response.read()
    finally:
        connection.close()
        server.shutdown()
        server.server_close()


def test_codex_checks_only_explicitly_and_sanitizes_errors(app, monkeypatch):
    def check(*args):
        raise RuntimeError("secret-test-key sensitive auth diagnostic")
    monkeypatch.setattr(webapp, "codex_preflight_status", check)
    response = app.handle("POST", "/api/connect", {"method": "codex"})
    assert response["auth"]["ready"] is False
    assert "secret-test-key" not in json.dumps(response)


def test_busy_run_freezes_settings_and_key(app):
    app.run = {"status": "running"}
    for route, payload in [("/api/connect", {"method": "deepseek", "api_key": "new"}), ("/api/preferences", {"remember_vault": False}), ("/api/estimate", {})]:
        with pytest.raises(webapp.RequestError, match="current operation"):
            app.handle("POST", route, payload)


def test_terminal_state_waits_for_provider_environment_restore(app, tmp_path, monkeypatch):
    restoring = threading.Event()
    release = threading.Event()

    class Environment(dict):
        def __setitem__(self, key, value):
            if key == "DEEPSEEK_API_KEY" and value == "previous-key":
                restoring.set()
                assert release.wait(2)
            super().__setitem__(key, value)

    environment = Environment(DEEPSEEK_API_KEY="previous-key")
    monkeypatch.setattr(webapp.os, "environ", environment)
    monkeypatch.setattr(webapp.api, "run_map", lambda *a, **kw: SimpleNamespace(to_dict=lambda: {"status": "complete"}))
    result = estimate(app, tmp_path)
    app.handle("POST", "/api/approve", {"estimate_id": result["estimate_id"], "allow_cloud": True})
    try:
        assert restoring.wait(2)
        assert app.run["status"] == "running"
    finally:
        release.set()
    wait_done(app)
    assert environment["DEEPSEEK_API_KEY"] == "previous-key"
    assert app.run["status"] == "completed"


def test_real_estimator_uses_scratch_and_no_provider_calls(tmp_path):
    app = webapp.LocalApp(preferences_path=tmp_path / "prefs.json", client=Client())
    app.handle("POST", "/api/connect", {"method": "deepseek", "api_key": "test-key"})
    result = estimate(app, tmp_path)
    assert result["estimate"]["provider_calls"] == 0
    assert result["estimate"]["inventory_count"] == 1
    assert result["estimate"]["pricing"]["status"] == "known"
    assert not Path(result["workspace"]).exists()


def test_terminal_partial_and_sensitive_diagnostic_redaction(app, tmp_path, monkeypatch):
    monkeypatch.setattr(webapp.api, "run_map", lambda *a, **kw: SimpleNamespace(to_dict=lambda: {"status": "complete", "parked_for_review_count": 1, "errors": [{"reason": "secret-test-key rejected"}]}))
    result = estimate(app, tmp_path)
    app.handle("POST", "/api/approve", {"estimate_id": result["estimate_id"], "allow_cloud": True})
    wait_done(app)
    state = app.handle("GET", "/api/run")
    assert state["status"] == "partial"
    assert "secret-test-key" not in json.dumps(state)


def test_folder_estimate_preserves_remembered_vault(app, tmp_path):
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    app.handle("POST", "/api/preferences", {"vault": str(vault), "remember_vault": True})
    estimate(app, tmp_path, destination="folder", remember_vault=True)
    assert app.preferences["vault"] == str(vault)


def test_codex_actionable_provider_error_redacts_previous_key(app, monkeypatch):
    def check(*args):
        raise webapp.ProviderError("unsupported Codex CLI version: 0.99; secret-test-key")
    monkeypatch.setattr(webapp, "codex_preflight_status", check)
    result = app.handle("POST", "/api/connect", {"method": "codex"})
    assert "unsupported Codex CLI version" in result["auth"]["message"]
    assert "secret-test-key" not in result["auth"]["message"]


def test_expired_approval_requires_new_estimate(app, tmp_path):
    result = estimate(app, tmp_path)
    app.ticket["created"] -= 1801
    with pytest.raises(webapp.RequestError, match="expired"):
        app.handle("POST", "/api/approve", {"estimate_id": result["estimate_id"], "allow_cloud": True})


def test_native_folder_picker_falls_back_to_manual_entry(monkeypatch):
    monkeypatch.setattr(webapp.sys, "platform", "linux")
    assert webapp.pick_folder("vault") == ""


def test_login_is_explicit_async_and_checks_after_completion(app, monkeypatch):
    entered = threading.Event()
    finish = threading.Event()
    calls = []
    class Process:
        def wait(self, timeout):
            entered.set()
            assert finish.wait(1)
            return 0
    monkeypatch.setattr(webapp, "_codex_environment", lambda: {})
    monkeypatch.setattr(webapp, "_codex_executable", lambda env: Path("/fake/codex"))
    monkeypatch.setattr(webapp, "_attach_codex_process_job", lambda process: None)
    monkeypatch.setattr(webapp.subprocess, "Popen", lambda command, **kw: calls.append(command) or Process())
    monkeypatch.setattr(webapp, "codex_preflight_status", lambda *args: {"status": "configured"})
    assert calls == []
    response = app.handle("POST", "/api/login")
    assert response["auth"]["status"] == "pending"
    assert entered.wait(1)
    with pytest.raises(webapp.RequestError):
        app.handle("POST", "/api/connect", {"method": "deepseek", "api_key": "other"})
    finish.set()
    for _ in range(100):
        if app.auth["status"] != "pending":
            break
        time.sleep(0.01)
    assert calls == [[str(Path("/fake/codex")), "login"]]
    assert app.auth["ready"] is True


def test_unmanaged_output_is_preserved(app, tmp_path):
    root = tmp_path / "output" / "my-research-abc12345"
    root.mkdir(parents=True)
    (root / "notes.md").write_text("human file")
    with pytest.raises(webapp.RequestError, match="unrelated"):
        estimate(app, tmp_path)
    assert (root / "notes.md").read_text() == "human file"
