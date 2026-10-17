from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from auto_zettelkasten import desktop, webapp
from auto_zettelkasten.cli import main as cli_main


class Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


def test_desktop_tls_uses_bundled_roots_and_preserves_explicit_trust(monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    monkeypatch.setitem(sys.modules, "certifi", SimpleNamespace(where=lambda: "/bundled/cacert.pem"))
    desktop._configure_tls()
    assert desktop.os.environ["SSL_CERT_FILE"] == "/bundled/cacert.pem"
    monkeypatch.setenv("SSL_CERT_FILE", "/custom/certificates.pem")
    desktop._configure_tls()
    assert desktop.os.environ["SSL_CERT_FILE"] == "/custom/certificates.pem"
    monkeypatch.delenv("SSL_CERT_FILE")
    monkeypatch.setenv("SSL_CERT_DIR", "/custom/certificate-directory")
    desktop._configure_tls()
    assert "SSL_CERT_FILE" not in desktop.os.environ


@pytest.mark.parametrize("platform,renderer", [("darwin", "cocoa"), ("win32", "edgechromium")])
def test_desktop_owns_server_native_picker_and_protects_running_job(tmp_path, monkeypatch, platform, renderer):
    created = []
    app = webapp.LocalApp(preferences_path=tmp_path / "preferences.json")
    event = Event()
    dialog_calls = []
    window = SimpleNamespace(
        events=SimpleNamespace(closing=event),
        create_file_dialog=lambda *a, **kw: [str(tmp_path)],
        create_confirmation_dialog=lambda *a: dialog_calls.append(a),
    )

    def create_window(title, url, **kwargs):
        created.append((title, url, kwargs))
        return window

    def start(**kwargs):
        assert kwargs["gui"] == renderer
        assert kwargs["private_mode"] is False
        assert event.handlers[0]() is True
        assert app.handle("POST", "/api/pick-folder", {"kind": "vault"})["path"] == str(tmp_path)
        app.run = {"status": "running"}
        assert event.handlers[0]() is False
        app.run = {"status": "idle"}
        app.auth["status"] = "pending"
        assert event.handlers[0]() is False

    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(create_window=create_window, start=start, FileDialog=SimpleNamespace(FOLDER=1)))
    monkeypatch.setattr(desktop, "LocalApp", lambda: app)
    monkeypatch.setattr(desktop, "_configure_tls", lambda: None)
    monkeypatch.setattr(desktop.sys, "platform", platform)
    desktop.launch()
    assert len(created) == 1
    assert created[0][0] == "Auto-Zettelkasten"
    assert created[0][1].startswith("http://127.0.0.1:")
    assert len(dialog_calls) == 2


def test_desktop_cli_dispatches_without_browser(monkeypatch):
    calls = []
    monkeypatch.setattr(desktop, "main", lambda args: calls.append(args) or 0)
    assert cli_main(["desktop"]) == 0
    assert calls == [[]]


def test_smoke_report_and_failure_exit_code(tmp_path, monkeypatch):
    report = tmp_path / "smoke.json"
    monkeypatch.setattr(desktop, "smoke_test", lambda: {"status": "passed"})
    assert desktop.main(["--smoke-test", "--smoke-report", str(report)]) == 0
    assert '"passed"' in report.read_text()
    monkeypatch.setattr(desktop, "smoke_test", lambda: (_ for _ in ()).throw(RuntimeError("missing bundle data")))
    assert desktop.main(["--smoke-test", "--smoke-report", str(report)]) == 1
    assert "missing bundle data" in report.read_text()


def test_missing_desktop_dependency_is_actionable(monkeypatch):
    monkeypatch.setitem(sys.modules, "webview", None)
    with pytest.raises(RuntimeError, match="desktop extra"):
        desktop.launch()


def test_estimate_existing_workspace_needs_no_symlinks(tmp_path, monkeypatch):
    class Client:
        def collections(self):
            return [{"key": "KEY1", "data": {"name": "Reading"}}]

        def inventory(self, *args):
            return [{"key": "DOC1"}]

    output = tmp_path / "output"
    workspace = output / "reading-key1"
    from auto_zettelkasten.api import initialize_workspace
    initialize_workspace(workspace)
    note = workspace / "02_source_memory" / "notes" / "Example.md"
    note.write_text("an estimator input")
    custody = workspace / "01_custody" / "files" / "document.pdf"
    custody.write_bytes(b"private source attachment")
    cost_path = workspace / "11_state" / "cost_estimate.yml"
    cost_path.write_text("previous estimate")
    monkeypatch.setattr(Path, "symlink_to", lambda *a, **kw: pytest.fail("Symlink privilege required"))

    def estimate(root, **kwargs):
        assert (root / "02_source_memory" / "notes" / note.name).read_text() == "an estimator input"
        assert not (root / "01_custody").exists()
        (root / "11_state").mkdir(parents=True, exist_ok=True)
        (root / "11_state" / "cost_estimate.yml").write_text("scratch estimate")
        return {"provider_calls": 0}

    monkeypatch.setattr(webapp.api, "estimate_cost", estimate)
    app = webapp.LocalApp(preferences_path=tmp_path / "prefs.json", client=Client())
    app.handle("POST", "/api/connect", {"method": "deepseek", "api_key": "test-key"})
    app.handle("POST", "/api/estimate", {"collection_key": "KEY1", "output_folder": str(output)})
    assert cost_path.read_text() == "previous estimate"
    assert custody.read_bytes() == b"private source attachment"
