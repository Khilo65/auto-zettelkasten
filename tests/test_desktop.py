from pathlib import Path
from types import SimpleNamespace
import sys
import threading

import pytest

from auto_zettelkasten import desktop, webapp
from auto_zettelkasten.cli import main as cli_main


class Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


@pytest.fixture
def close_checks(monkeypatch):
    constructor = threading.Thread
    checks = []

    def track(*args, **kwargs):
        thread = constructor(*args, **kwargs)
        if kwargs.get("name") == "desktop-close-check":
            checks.append(thread)
        return thread

    monkeypatch.setattr(desktop.threading, "Thread", track)

    def wait():
        assert checks
        checks[-1].join(timeout=2)
        assert not checks[-1].is_alive()

    return wait


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


def test_cocoa_context_menu_keeps_native_editing_actions_without_debug(monkeypatch):
    class Host:
        pass

    browser = SimpleNamespace(WebKitHost=Host)
    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "webview.platforms.cocoa", SimpleNamespace(BrowserView=browser))
    monkeypatch.setitem(sys.modules, "WebKit", SimpleNamespace(
        WebMenuItemTagCopy=8, WebMenuItemTagCut=13, WebMenuItemTagPaste=14,
    ))
    replacements = []

    def selector(method, **kwargs):
        assert kwargs == {"selector": b"willOpenMenu:withEvent:", "signature": b"v@:@@"}
        return method

    def add_methods(host, methods):
        replacements.append(host)
        for method in methods:
            setattr(host, method.__name__, method)

    monkeypatch.setitem(sys.modules, "objc", SimpleNamespace(selector=selector, classAddMethods=add_methods))
    copy = SimpleNamespace(tag=lambda: 8, title="Copier", target=object(), enabled=True)
    cut = SimpleNamespace(tag=lambda: 13, title="Cut", target=object(), enabled=False)
    paste = SimpleNamespace(tag=lambda: 14, title="Paste", target=object(), enabled=True)
    items = [SimpleNamespace(tag=lambda: 99), copy, SimpleNamespace(tag=lambda: 3), cut, paste]
    menu = SimpleNamespace(itemArray=lambda: items, removeItem_=items.remove)
    original_targets = [item.target for item in (copy, cut, paste)]

    desktop._configure_editing_menu()
    desktop._configure_editing_menu()
    assert browser.WebKitHost is Host
    assert replacements == [Host]
    Host().willOpenMenu_withEvent_(menu, object())
    assert items == [copy, cut, paste]
    assert [item.target for item in items] == original_targets
    assert cut.enabled is False
    items.clear()
    Host().willOpenMenu_withEvent_(menu, object())
    assert items == []


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_native_editing_menu_does_not_import_cocoa_on_other_platforms(monkeypatch, platform):
    monkeypatch.setattr(desktop.sys, "platform", platform)
    monkeypatch.setitem(sys.modules, "webview.platforms.cocoa", None)
    desktop._configure_editing_menu()


@pytest.mark.parametrize("platform,renderer", [("darwin", "cocoa"), ("win32", "edgechromium")])
def test_desktop_owns_server_native_picker_and_protects_running_job(tmp_path, monkeypatch, platform, renderer, close_checks):
    created = []
    editing_menu_calls = []
    view_zooms = []
    app = webapp.LocalApp(preferences_path=tmp_path / "preferences.json")
    event = Event()
    dialog_calls = []
    window = SimpleNamespace(
        events=SimpleNamespace(closing=event),
        create_file_dialog=lambda *a, **kw: [str(tmp_path)],
        create_confirmation_dialog=lambda *a: dialog_calls.append(a),
        run_js=lambda script: "legacy" if "workspacePrepareClose" in script else '"clean"' if platform == "win32" else "clean",
        destroy=lambda: event.handlers[0](),
    )

    def create_window(title, url, **kwargs):
        created.append((title, url, kwargs))
        return window

    def start(**kwargs):
        assert kwargs["gui"] == renderer
        assert callable(app.view_zoom)
        assert app.view_zoom(1.2) == 1.2
        assert Path(kwargs["icon"]).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        assert kwargs["private_mode"] is False
        assert event.handlers[0]() is False
        close_checks()
        assert event.handlers[0]() is True
        assert app.handle("POST", "/api/pick-folder", {"kind": "vault"})["path"] == str(tmp_path)
        app.run = {"status": "running"}
        assert event.handlers[0]() is False
        close_checks()
        app.run = {"status": "idle"}
        app.auth["status"] = "pending"
        assert event.handlers[0]() is False
        close_checks()

    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(create_window=create_window, start=start, FileDialog=SimpleNamespace(FOLDER=1)))
    monkeypatch.setattr(desktop, "LocalApp", lambda: app)
    monkeypatch.setattr(desktop, "_configure_tls", lambda: None)
    monkeypatch.setattr(desktop, "_configure_editing_menu", lambda: editing_menu_calls.append(True))
    monkeypatch.setattr(desktop, "_set_view_zoom", lambda candidate, level: view_zooms.append((candidate, level)) or level)
    monkeypatch.setattr(desktop.sys, "platform", platform)
    desktop.launch()
    assert len(created) == 1
    assert created[0][0] == "Auto-Zettelkasten"
    assert created[0][1].startswith("http://127.0.0.1:")
    assert created[0][2]["zoomable"] is True
    assert view_zooms == [(window, 1.2)]
    assert editing_menu_calls == [True]
    assert len(dialog_calls) == 2


@pytest.mark.parametrize("level", [True, False, None, "1.2", float("nan"), float("inf"), -float("inf"), 0.49, 2.01, 10 ** 1000])
def test_native_view_zoom_rejects_invalid_levels(level):
    with pytest.raises(ValueError, match="View zoom"):
        desktop._set_view_zoom(SimpleNamespace(), level)


def test_native_view_zoom_applies_cocoa_page_zoom_without_changing_pinch(monkeypatch):
    calls = []
    native = SimpleNamespace(contentView=lambda: SimpleNamespace(setPageZoom_=calls.append))
    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    assert desktop._set_view_zoom(SimpleNamespace(native=native), 1.3) == 1.3
    assert calls == [1.3]


def test_native_view_zoom_applies_webview2_zoom_on_ui_thread(monkeypatch):
    native = SimpleNamespace(InvokeRequired=False, webview=SimpleNamespace(ZoomFactor=1))
    monkeypatch.setattr(desktop.sys, "platform", "win32")
    assert desktop._set_view_zoom(SimpleNamespace(native=native), 0.8) == 0.8
    assert native.webview.ZoomFactor == 0.8


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_native_view_zoom_dispatches_to_ui_and_waits_for_completion(monkeypatch, platform):
    queued = threading.Event()
    callbacks, calls, results = [], [], []

    def queue(callback):
        callbacks.append(callback)
        queued.set()

    if platform == "darwin":
        native = SimpleNamespace(contentView=lambda: SimpleNamespace(setPageZoom_=lambda level: calls.append((level, threading.current_thread()))))
        monkeypatch.setitem(sys.modules, "PyObjCTools", SimpleNamespace(AppHelper=SimpleNamespace(callAfter=queue)))
    else:
        class View:
            @property
            def ZoomFactor(self):
                return 1

            @ZoomFactor.setter
            def ZoomFactor(self, level):
                calls.append((level, threading.current_thread()))

        native = SimpleNamespace(InvokeRequired=True, BeginInvoke=queue, webview=View())
        monkeypatch.setitem(sys.modules, "System", SimpleNamespace(Action=lambda callback: callback))
    monkeypatch.setattr(desktop.sys, "platform", platform)
    worker = threading.Thread(target=lambda: results.append(desktop._set_view_zoom(SimpleNamespace(native=native), 1.2)))
    worker.start()
    try:
        assert queued.wait(2)
        assert not results and not calls
        callbacks[0]()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert results == [1.2]
        assert calls == [(1.2, threading.current_thread())]
    finally:
        if worker.is_alive() and callbacks:
            callbacks[0]()
        worker.join(timeout=2)


def test_native_view_zoom_reports_native_failure(monkeypatch):
    def unavailable(level):
        raise RuntimeError("window closed")

    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    native = SimpleNamespace(contentView=lambda: SimpleNamespace(setPageZoom_=unavailable))
    with pytest.raises(RuntimeError, match="window closed"):
        desktop._set_view_zoom(SimpleNamespace(native=native), 1.2)
    with pytest.raises(RuntimeError, match="native view"):
        desktop._set_view_zoom(SimpleNamespace(native=None), 1.2)


def test_native_view_zoom_does_not_apply_a_queued_request_after_timeout(monkeypatch):
    callbacks, calls = [], []
    native = SimpleNamespace(contentView=lambda: SimpleNamespace(setPageZoom_=calls.append))
    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "PyObjCTools", SimpleNamespace(AppHelper=SimpleNamespace(callAfter=callbacks.append)))
    with monkeypatch.context() as patch:
        patch.setattr(desktop.threading, "current_thread", lambda: object())
        patch.setattr(desktop.threading.Event, "wait", lambda self, timeout: False)
        with pytest.raises(RuntimeError, match="respond"):
            desktop._set_view_zoom(SimpleNamespace(native=native), 1.2)
    assert len(callbacks) == 1
    callbacks[0]()
    assert not calls


def test_native_view_zoom_reports_unsupported_platform(monkeypatch):
    monkeypatch.setattr(desktop.sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="not supported"):
        desktop._set_view_zoom(SimpleNamespace(native=object()), 1.2)


def test_desktop_cli_dispatches_without_browser(monkeypatch):
    calls = []
    monkeypatch.setattr(desktop, "main", lambda args: calls.append(args) or 0)
    assert cli_main(["desktop"]) == 0
    assert calls == [[]]


def test_desktop_close_protects_unsaved_notes_and_failed_checks(tmp_path, monkeypatch, close_checks):
    app = webapp.LocalApp(preferences_path=tmp_path / "preferences.json")
    event = Event()
    state = {"dirty": True, "discard": False, "failed": False}
    dialogs = []
    destroyed = []

    def evaluate(script):
        assert threading.current_thread() is not threading.main_thread()
        if "workspacePrepareClose" in script:
            return "legacy"
        if "workspaceCancelClose" in script:
            return
        assert "workspaceHasUnsavedChanges" in script
        if state["failed"]:
            raise RuntimeError("Editor did not respond")
        return "dirty" if state["dirty"] else "clean"

    window = SimpleNamespace(
        events=SimpleNamespace(closing=event),
        run_js=evaluate,
        create_confirmation_dialog=lambda title, text: dialogs.append(title) or state["discard"],
        destroy=lambda: destroyed.append(event.handlers[0]()),
    )

    def start(**kwargs):
        close = event.handlers[0]
        assert close() is False
        close_checks()
        assert not destroyed
        state["failed"] = True
        assert close() is False
        close_checks()
        assert not destroyed
        state.update(failed=False, discard=True)
        assert close() is False
        close_checks()
        assert destroyed == [True]

    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(create_window=lambda *a, **kw: window, start=start))
    monkeypatch.setattr(desktop, "LocalApp", lambda: app)
    monkeypatch.setattr(desktop, "_configure_tls", lambda: None)
    monkeypatch.setattr(desktop, "_configure_editing_menu", lambda: None)
    desktop.launch()
    assert dialogs == ["Unsaved notes", "Unable to check unsaved notes", "Unsaved notes"]


def test_desktop_close_flushes_pdf_before_checking_notes(tmp_path, monkeypatch, close_checks):
    app = webapp.LocalApp(preferences_path=tmp_path / "preferences.json")
    event, calls, destroyed, dialogs = Event(), [], [], []
    prepared = iter(['"waiting"', '"ready"'])

    def evaluate(script):
        assert threading.current_thread() is not threading.main_thread()
        if "workspacePrepareClose" in script:
            calls.append("prepare")
            return next(prepared)
        if "workspaceHasUnsavedChanges" in script:
            assert calls == ["prepare", "prepare"]
            calls.append("notes")
            return "clean"
        if "workspaceCancelClose" in script:
            calls.append("cancel")

    window = SimpleNamespace(events=SimpleNamespace(closing=event), run_js=evaluate,
        create_confirmation_dialog=lambda *args: dialogs.append(args) or False,
        destroy=lambda: destroyed.append(event.handlers[0]()))

    def start(**kwargs):
        assert event.handlers[0]() is False
        close_checks()
        assert destroyed == [True]

    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(create_window=lambda *args, **kwargs: window, start=start))
    monkeypatch.setattr(desktop, "LocalApp", lambda: app)
    monkeypatch.setattr(desktop, "_configure_tls", lambda: None)
    monkeypatch.setattr(desktop, "_configure_editing_menu", lambda: None)
    desktop.launch()
    assert calls == ["prepare", "prepare", "notes"] and dialogs == []


@pytest.mark.parametrize("scenario,expected_dialog", [
    ("blocked", "PDF changes need attention"),
    ("timeout", "PDF changes need attention"),
    ("error", "Unable to check unsaved notes"),
    ("cancel", "Unsaved notes"),
    ("operation", "Operation in progress"),
])
def test_desktop_close_restores_editor_after_aborted_pdf_preparation(tmp_path, monkeypatch, close_checks, scenario, expected_dialog):
    app = webapp.LocalApp(preferences_path=tmp_path / "preferences.json")
    event, calls, destroyed, dialogs = Event(), [], [], []

    def evaluate(script):
        if "workspacePrepareClose" in script:
            calls.append("prepare")
            if scenario == "error":
                raise RuntimeError("Reader unavailable")
            if scenario == "operation":
                app.run["status"] = "running"
                return "waiting"
            return "ready" if scenario == "cancel" else "waiting" if scenario == "timeout" else "blocked"
        if "workspaceHasUnsavedChanges" in script:
            calls.append("notes")
            return "dirty"
        if "workspaceCancelClose" in script:
            calls.append("cancel")

    window = SimpleNamespace(events=SimpleNamespace(closing=event), run_js=evaluate,
        create_confirmation_dialog=lambda title, text: dialogs.append((title, text)) or False,
        destroy=lambda: destroyed.append(True))

    def start(**kwargs):
        assert event.handlers[0]() is False
        close_checks()
        assert destroyed == []

    if scenario == "timeout":
        clock = iter([0, 10])
        monkeypatch.setattr(desktop, "time", SimpleNamespace(monotonic=lambda: next(clock), sleep=lambda duration: pytest.fail("Expired preparation must not sleep")))
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(create_window=lambda *args, **kwargs: window, start=start))
    monkeypatch.setattr(desktop, "LocalApp", lambda: app)
    monkeypatch.setattr(desktop, "_configure_tls", lambda: None)
    monkeypatch.setattr(desktop, "_configure_editing_menu", lambda: None)
    desktop.launch()
    assert calls[-1] == "cancel" and len(dialogs) == 1 and dialogs[0][0] == expected_dialog
    if scenario in {"blocked", "timeout"}:
        assert "Close and keep changes pending" in dialogs[0][1]
    assert ("notes" in calls) is (scenario == "cancel")


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
