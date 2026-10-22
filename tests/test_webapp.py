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
    monkeypatch.setattr(webapp.api, "estimate_cost", lambda *a, **kw: {"inventory_count": len(kw["zotero_client"].inventory(kw["scope"], kw.get("collection_key") or None)), "cost": {"expected_usd": 0.5}})
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


class ScopedClient:
    def __init__(self):
        self.rows = [
            {"key": "PARENT", "data": {"name": "Thesis", "parentCollection": False}},
            {"key": "CHILD", "data": {"name": "Reading", "parentCollection": "PARENT"}},
            {"key": "GRANDCHILD", "data": {"name": "Methods", "parentCollection": "CHILD"}},
            {"key": "SIBLING", "data": {"name": "Unrelated"}},
        ]
        self.library = [{"key": key, "data": {"title": key}} for key in ("DOC1", "DOC2", "DOC3")]
        self.members = {"PARENT": [], "CHILD": self.library[:1], "GRANDCHILD": self.library[:2], "SIBLING": self.library[2:]}
        self.calls = []

    def collections(self):
        return self.rows

    def inventory(self, scope, collection_key=None):
        self.calls.append((scope, collection_key))
        return self.library if scope == "library" else self.members[collection_key]

    def children(self, key):
        return [{"key": "ATTACHMENT", "data": {"parentItem": key}}]


def test_library_scope_with_zero_collections_uses_real_estimator_without_provider_or_destination(tmp_path, monkeypatch):
    client = ScopedClient()
    client.rows = []
    local = webapp.LocalApp(preferences_path=tmp_path / "prefs.json", client=client)
    local.handle("POST", "/api/connect", {"method": "deepseek", "api_key": "local-test-key"})
    monkeypatch.setattr(webapp.api, "run_map", lambda *a, **kw: pytest.fail("No mapping before approval"))
    result = local.handle("POST", "/api/estimate", {"scope": "library", "output_folder": str(tmp_path / "output")})
    assert result["source"] == {"scope": "library", "key": "", "name": "Entire Zotero library", "path": "Entire Zotero library",
                                "include_subcollections": False, "collection_keys": [], "item_count": 3, "library_id": "0"}
    assert result["collection"]["name"] == "Entire Zotero library"
    assert result["workspace"] == str(tmp_path / "output" / "zotero-library")
    assert result["estimate"]["inventory_count"] == 3 and result["estimate"]["provider_calls"] == 0
    assert not (tmp_path / "output").exists()
    assert client.calls == [("library", None)]


def test_collection_default_remains_direct_and_descendants_are_opt_in(app, tmp_path):
    client = app.client = ScopedClient()
    with pytest.raises(webapp.RequestError, match="contains no documents"):
        estimate(app, tmp_path, collection_key="PARENT")
    assert client.calls == [("library", None), ("collection", "PARENT")]
    result = estimate(app, tmp_path, collection_key="PARENT", include_subcollections=True)
    assert result["source"]["collection_keys"] == ["PARENT", "CHILD", "GRANDCHILD"]
    assert result["source"]["item_count"] == 2
    assert result["estimate"]["inventory_count"] == 2
    assert app.ticket["request"].scope == "collection" and app.ticket["request"].collection_key == "PARENT"
    assert [row["key"] for row in app.ticket["client"].items] == ["DOC1", "DOC2"]
    assert ("collection", "SIBLING") not in client.calls
    direct = estimate(app, tmp_path, collection_key="CHILD")
    assert direct["source"]["collection_keys"] == ["CHILD"]
    assert direct["source"]["path"] == "Thesis / Reading"
    assert direct["estimate"]["inventory_count"] == 1


@pytest.mark.parametrize("scope,descendants", [("library", False), ("collection", False), ("collection", True)])
def test_all_source_scopes_freeze_members_metadata_hierarchy_and_engine_request(app, tmp_path, monkeypatch, scope, descendants):
    client = app.client = ScopedClient()
    key = None if scope == "library" else "CHILD"
    result = estimate(app, tmp_path, scope=scope, collection_key=key, include_subcollections=descendants)
    expected_keys = [row["key"] for row in app.ticket["client"].items]
    client.library.append({"key": "LATE", "data": {"title": "Unapproved"}})
    client.library[0]["data"]["title"] = "Changed after approval"
    client.rows[1]["data"]["name"] = "Renamed after approval"

    def unexpected_inventory(*args):
        pytest.fail("The mapper consulted live source membership after approval")

    client.inventory = unexpected_inventory
    calls = []

    def mapper(request, *, client, run_id):
        rows = client.inventory(request.scope, request.collection_key)
        calls.append((request, [row["key"] for row in rows]))
        assert [row["key"] for row in client.inventory("library")] == ["DOC1", "DOC2", "DOC3"]
        assert client.inventory("library")[0]["data"]["title"] == "DOC1"
        assert client.collections()[1]["data"]["name"] == "Reading"
        rows[0]["data"]["title"] = "Caller mutation"
        assert client.inventory(request.scope, request.collection_key)[0]["data"]["title"] == "DOC1"
        assert client.children("DOC1")[0]["data"]["parentItem"] == "DOC1"
        request.workspace.mkdir(parents=True)
        (request.workspace / "auto-zettelkasten.yml").write_text("engine_version: test\n")
        return SimpleNamespace(to_dict=lambda: {"status": "complete"})

    monkeypatch.setattr(webapp.api, "run_map", mapper)
    approved = app.handle("POST", "/api/approve", {"estimate_id": result["estimate_id"], "allow_cloud": True})
    assert approved["source"]["scope"] == scope
    wait_done(app)
    assert len(calls) == 1 and calls[0][1] == expected_keys
    assert calls[0][0].scope == scope and calls[0][0].collection_key == key
    state = app.handle("GET", "/api/run")
    assert state["status"] == "completed" and state["workspace_available"]
    assert state["memory_workspace"] == result["workspace"]
    assert state["source"] == result["source"]


@pytest.mark.parametrize("fields", [
    {"scope": "selected"}, {"scope": "folder"}, {"scope": None}, {"scope": []},
    {"include_subcollections": "true"}, {"include_subcollections": 1}, {"include_subcollections": None},
    {"remember_vault": "false"}, {"scope": "library", "collection_key": "ABC12345"},
    {"scope": "library", "collection_key": None, "include_subcollections": True},
])
def test_invalid_scope_or_boolean_fields_clear_old_approval_without_guessing(app, tmp_path, fields):
    previous = estimate(app, tmp_path)
    with pytest.raises(webapp.RequestError):
        estimate(app, tmp_path, **fields)
    assert app.ticket is None
    with pytest.raises(webapp.RequestError, match="expired"):
        app.handle("POST", "/api/approve", {"estimate_id": previous["estimate_id"], "allow_cloud": True})


@pytest.mark.parametrize("rows", [
    [{"key": "A", "data": {"name": "A", "parentCollection": "B"}}, {"key": "B", "data": {"name": "B", "parentCollection": "A"}}],
    [{"key": "A", "data": {"name": "One"}}, {"key": "A", "data": {"name": "Two"}}],
    [{"key": "A", "data": {"name": "A", "parentCollection": "MISSING"}}],
    [{"key": "A", "data": {"name": "A"}}, {"key": "a", "data": {"name": "Alias"}}],
])
def test_invalid_collection_hierarchies_fail_closed(app, tmp_path, rows):
    client = app.client = ScopedClient()
    client.rows = rows
    with pytest.raises(webapp.RequestError):
        estimate(app, tmp_path, collection_key="A", include_subcollections=True)
    assert client.calls == []


def test_hierarchy_depth_bound_counts_already_settled_ancestors(app, tmp_path):
    client = app.client = ScopedClient()
    client.rows = [{"key": f"KEY{number}", "data": {"name": "Nested", "parentCollection": f"KEY{number - 1}" if number else False}} for number in range(257)]
    with pytest.raises(webapp.RequestError, match="depth limit"):
        estimate(app, tmp_path, collection_key="KEY0", include_subcollections=True)
    assert client.calls == []


def test_unknown_collection_does_not_fall_back_to_library_and_nonpersonal_library_is_refused(app, tmp_path):
    client = app.client = ScopedClient()
    with pytest.raises(webapp.RequestError, match="current inventory"):
        estimate(app, tmp_path, collection_key="STALE")
    assert client.calls == []
    client.library_id = "12345"
    with pytest.raises(webapp.RequestError, match="personal Zotero"):
        estimate(app, tmp_path, scope="library", collection_key=None)


def test_frozen_source_refuses_scope_expansion_and_collection_metadata_is_fixed(app, tmp_path):
    app.client = ScopedClient()
    estimate(app, tmp_path, collection_key="CHILD")
    frozen = app.ticket["client"]
    for scope, key in [("selected", None), ("collection", "SIBLING"), ("library", "CHILD")]:
        with pytest.raises(webapp.RequestError, match="approved source"):
            frozen.inventory(scope, key)


def test_library_cancel_auth_change_and_new_scope_require_fresh_approval(app, tmp_path):
    first = estimate(app, tmp_path, scope="library", collection_key=None)
    app.handle("POST", "/api/cancel", {"estimate_id": first["estimate_id"]})
    with pytest.raises(webapp.RequestError):
        app.handle("POST", "/api/approve", {"estimate_id": first["estimate_id"], "allow_cloud": True})
    second = estimate(app, tmp_path, scope="library", collection_key=None)
    current = estimate(app, tmp_path)
    with pytest.raises(webapp.RequestError):
        app.handle("POST", "/api/approve", {"estimate_id": second["estimate_id"], "allow_cloud": True})
    app.handle("POST", "/api/connect", {"method": "deepseek", "api_key": "new-key"})
    with pytest.raises(webapp.RequestError):
        app.handle("POST", "/api/approve", {"estimate_id": current["estimate_id"], "allow_cloud": True})


def test_export_exception_retains_partial_memory_report_and_openable_workspace(app, tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    result = estimate(app, tmp_path, scope="library", collection_key=None, destination="obsidian", vault=str(vault))

    def mapper(request, **kwargs):
        request.workspace.mkdir(parents=True)
        (request.workspace / "auto-zettelkasten.yml").write_text("engine_version: test\n")
        return SimpleNamespace(to_dict=lambda: {"status": "complete", "validated_note_count": 2})

    def export_failure(*args, **kwargs):
        raise RuntimeError("secret-test-key private export diagnostic")

    monkeypatch.setattr(webapp.api, "run_map", mapper)
    monkeypatch.setattr(webapp.api, "export_to_obsidian", export_failure)
    app.handle("POST", "/api/approve", {"estimate_id": result["estimate_id"], "allow_cloud": True})
    wait_done(app)
    state = app.handle("GET", "/api/run")
    assert state["status"] == "partial" and state["report"]["validated_note_count"] == 2
    assert state["workspace_available"] and state["memory_workspace"] == result["workspace"]
    assert "export did not finish" in state["export_error"]
    assert "private export diagnostic" not in json.dumps(state) and "secret-test-key" not in json.dumps(state)


@pytest.mark.parametrize("scope,key", [("library", None), ("collection", "ABC12345")])
def test_existing_workspace_estimate_keeps_source_metadata_and_only_copies_inputs(app, tmp_path, scope, key):
    first = estimate(app, tmp_path, scope=scope, collection_key=key)
    workspace = Path(first["workspace"])
    notes = workspace / "02_source_memory" / "notes"
    notes.mkdir(parents=True)
    (workspace / "auto-zettelkasten.yml").write_text("engine_version: test\n")
    note = notes / "Existing.md"
    note.write_bytes(b"# Existing\r\n")
    second = estimate(app, tmp_path, scope=scope, collection_key=key)
    assert second["source"] == first["source"]
    assert json.loads(json.dumps(second))["source"]["scope"] == scope
    assert note.read_bytes() == b"# Existing\r\n"


@pytest.mark.parametrize("backend", ["local", "llm"])
def test_estimate_retains_workspace_mapping_route_in_approval_ticket(app, tmp_path, backend):
    from auto_zettelkasten.files import write_yaml

    first = estimate(app, tmp_path)
    workspace = Path(first["workspace"])
    workspace.mkdir(parents=True)
    config = workspace / "auto-zettelkasten.yml"
    write_yaml(config, {
        "mapping_backend": backend,
        "local_mapping": {"clustering": "hdbscan", "model_path": str(tmp_path / "cached-model")},
    })
    before = config.read_bytes()
    second = estimate(app, tmp_path)
    assert app.ticket["request"].mapping_backend == backend
    assert app.ticket["request"].local_mapping["clustering"] == "hdbscan"
    assert app.ticket["request"].local_mapping["model_path"] == str(tmp_path / "cached-model")
    assert second["source"] == first["source"]
    assert config.read_bytes() == before
    assert not (workspace / "11_state" / "workspace_manifest.yml").exists()


def test_selected_embedding_model_is_checked_frozen_and_remembered(app, tmp_path, monkeypatch):
    from auto_zettelkasten import mapping

    model = tmp_path / "cached-minilm"
    model.mkdir()
    checked = []
    monkeypatch.setattr(mapping, "check_local_mapping_availability", lambda options: checked.append(dict(options)))
    result = estimate(app, tmp_path, embedding_model_path=str(model))
    selected = str(model.resolve())
    assert checked and checked[0]["model_path"] == selected
    assert app.ticket["request"].local_mapping["model_path"] == selected
    assert app.handle("GET", "/api/state")["embedding_model_path"] == selected
    assert not Path(result["workspace"]).exists()
    app.handle("POST", "/api/preferences", {"remember_vault": False})
    reopened = webapp.LocalApp(preferences_path=app.preferences_path, client=Client())
    assert reopened.embedding_model_path == selected
    assert "secret-test-key" not in app.preferences_path.read_text()


def test_remembered_model_is_a_default_and_explicit_selection_overrides_workspace(app, tmp_path, monkeypatch):
    from auto_zettelkasten import mapping
    from auto_zettelkasten.files import write_yaml

    first = estimate(app, tmp_path)
    workspace = Path(first["workspace"])
    workspace.mkdir(parents=True)
    model = tmp_path / "chosen-minilm"
    model.mkdir()
    configured = tmp_path / "workspace-minilm"
    configured.mkdir()
    app.embedding_model_path = str(model.resolve())
    monkeypatch.setattr(mapping, "check_local_mapping_availability", lambda options: None)
    write_yaml(workspace / "auto-zettelkasten.yml", {
        "mapping_backend": "local", "local_mapping": {"model_path": str(configured)},
    })
    before = (workspace / "auto-zettelkasten.yml").read_bytes()
    estimate(app, tmp_path)
    assert app.ticket["request"].local_mapping["model_path"] == str(configured)
    estimate(app, tmp_path, embedding_model_path=str(model))
    assert app.ticket["request"].local_mapping["model_path"] == str(model.resolve())
    assert (workspace / "auto-zettelkasten.yml").read_bytes() == before


def test_clearing_model_selection_forgets_it_without_reusing_the_old_path(app, tmp_path):
    app.embedding_model_path = str(tmp_path / "old-model")
    estimate(app, tmp_path, embedding_model_path="")
    assert app.ticket["request"].local_mapping["model_path"] is None
    assert webapp.LocalApp(preferences_path=app.preferences_path, client=Client()).embedding_model_path == ""


@pytest.mark.parametrize("value", [[], 42, "bad\x00path", "x" * 4097], ids=["list", "number", "nul", "too-long"])
def test_invalid_model_selection_cannot_create_an_approval_ticket(app, tmp_path, value):
    with pytest.raises(webapp.RequestError, match="model folder"):
        estimate(app, tmp_path, embedding_model_path=value)
    assert app.ticket is None
    assert not (tmp_path / "output").exists()


def test_unavailable_model_is_rejected_before_approval_or_preference_write(app, tmp_path, monkeypatch):
    from auto_zettelkasten import mapping
    from auto_zettelkasten.local_mapping import LocalMappingError

    def unavailable(options):
        raise LocalMappingError("The selected model is incomplete.")

    monkeypatch.setattr(mapping, "check_local_mapping_availability", unavailable)
    with pytest.raises(webapp.RequestError, match="selected model is incomplete"):
        estimate(app, tmp_path, embedding_model_path=str(tmp_path / "missing"))
    assert app.ticket is None
    assert not app.preferences_path.exists()
    assert not (tmp_path / "output").exists()


def test_model_picker_uses_native_folder_selection(app):
    picked = []
    app.folder_picker = lambda kind: picked.append(kind) or "/cached/minilm"
    assert app.handle("POST", "/api/pick-folder", {"kind": "embedding-model"}) == {"path": "/cached/minilm"}
    assert picked == ["embedding-model"]


@pytest.mark.parametrize("change", ["membership", "metadata", "outer-version", "data-version"])
def test_collection_estimate_rejects_same_key_changes_after_library_snapshot(app, tmp_path, monkeypatch, change):
    client = app.client = ScopedClient()
    client.library[0]["data"]["collections"] = ["SIBLING"]
    client.library[0]["version"] = 1
    client.library[0]["data"]["version"] = 1
    original_inventory = client.inventory

    def changing_inventory(scope, collection_key=None):
        if scope == "collection":
            # The same object is reused by this client; the earlier library read
            # must already be owned by the estimate rather than this mutation.
            item = client.library[0]
            if change == "membership":
                item["data"]["collections"] = ["CHILD"]
            elif change == "metadata":
                item["data"]["title"] = "Changed title"
            elif change == "outer-version":
                item["version"] = 2
            else:
                item["data"]["version"] = 2
        return original_inventory(scope, collection_key)

    client.inventory = changing_inventory
    monkeypatch.setattr(webapp.api, "estimate_cost", lambda *args, **kwargs: pytest.fail("Mixed source snapshots must not be estimated"))
    with pytest.raises(webapp.RequestError, match="changed during inventory"):
        estimate(app, tmp_path, collection_key="CHILD")
    assert app.ticket is None
    assert not (tmp_path / "output").exists()
