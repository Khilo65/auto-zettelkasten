"""Native desktop window around the existing approval-gated local application."""
from __future__ import annotations

import argparse
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from .webapp import LocalApp, LocalServer


def _configure_tls() -> None:
    if not os.environ.get("SSL_CERT_FILE") and not os.environ.get("SSL_CERT_DIR"):
        import certifi

        os.environ["SSL_CERT_FILE"] = certifi.where()


def _set_view_zoom(window, level: float) -> float:
    if type(level) not in (int, float) or not 0.5 <= level <= 2:
        raise ValueError("View zoom must be a number between 0.5 and 2.")
    if sys.platform not in {"darwin", "win32"}:
        raise RuntimeError("Native view zoom is not supported on this platform.")

    completed = threading.Event()
    cancelled = threading.Event()
    errors = []

    def apply():
        try:
            if cancelled.is_set():
                return
            if sys.platform == "darwin":
                window.native.contentView().setPageZoom_(level)
            else:
                window.native.webview.ZoomFactor = level
        except Exception as exc:
            errors.append(exc)
        finally:
            completed.set()

    try:
        if sys.platform == "darwin" and threading.current_thread() is not threading.main_thread():
            from PyObjCTools import AppHelper

            AppHelper.callAfter(apply)
        elif sys.platform == "win32" and window.native.InvokeRequired:
            from System import Action

            window.native.BeginInvoke(Action(apply))
        else:
            apply()
    except Exception as exc:
        raise RuntimeError(f"Could not change the native view size: {exc}") from exc
    if not completed.wait(5):
        cancelled.set()
        raise RuntimeError("The native view did not respond to the size change.")
    if errors:
        raise RuntimeError(f"Could not change the native view size: {errors[0]}") from errors[0]
    return level


def launch() -> None:
    try:
        import webview
    except ImportError as exc:
        raise RuntimeError("Install the desktop extra: python -m pip install 'auto-zettelkasten[desktop]'.") from exc

    _configure_tls()
    app = LocalApp()
    with LocalServer(("127.0.0.1", 0), app=app) as server:
        window = webview.create_window(
            "Auto-Zettelkasten",
            f"http://127.0.0.1:{server.server_port}",
            width=1240,
            height=860,
            min_size=(560, 640),
            text_select=True,
            zoomable=True,
            background_color="#fcfbf9",
        )
        app.view_zoom = lambda level: _set_view_zoom(window, level)

        def pick(kind: str) -> str:
            if kind == "codex":
                selection = window.create_file_dialog(webview.FileDialog.OPEN, directory=str(Path.home()), allow_multiple=False)
                return str(selection[0]) if selection else ""
            previous = app.embedding_model_path if kind == "embedding-model" else app.preferences.get("vault", "") if kind == "vault" else ""
            directory = previous if previous and Path(previous).is_dir() else str(Path.home())
            selection = window.create_file_dialog(webview.FileDialog.FOLDER, directory=directory, allow_multiple=False)
            return str(selection[0]) if selection else ""

        close_ready = threading.Event()
        close_pending = threading.Lock()

        def operation_in_progress():
            return app.run["status"] == "running" or app.auth["status"] == "pending"

        def check_close():
            try:
                if operation_in_progress():
                    window.create_confirmation_dialog("Operation in progress", "Keep this window open until mapping or sign-in finishes. You can minimize it while you wait.")
                    return
                try:
                    # evaluate_js wraps eval, which the local app's script policy forbids.
                    result = window.run_js("window.workspaceHasUnsavedChanges ? (window.workspaceHasUnsavedChanges() ? 'dirty' : 'clean') : 'unavailable'")
                    if result not in ("dirty", "clean") and isinstance(result, str):
                        result = json.loads(result)
                    if result not in ("dirty", "clean"):
                        raise ValueError("Editor state is unavailable")
                    dirty = result == "dirty"
                except Exception:
                    window.create_confirmation_dialog("Unable to check unsaved notes", "Return to the editor and save your notes before closing.")
                    return
                if dirty and not window.create_confirmation_dialog("Unsaved notes", "Some notes have unsaved changes. Close without saving?"):
                    return
                if not operation_in_progress():
                    close_ready.set()
                    try:
                        window.destroy()
                    except Exception:
                        close_ready.clear()
            finally:
                close_pending.release()

        def closing():
            if close_ready.is_set() and not operation_in_progress():
                return True
            close_ready.clear()
            # Cocoa must return from this event before evaluating JS or showing dialogs.
            if close_pending.acquire(blocking=False):
                threading.Thread(target=check_close, name="desktop-close-check", daemon=True).start()
            return False

        app.folder_picker = pick
        window.events.closing += closing
        worker = threading.Thread(target=server.serve_forever, name="desktop-local-server", daemon=True)
        worker.start()
        try:
            webview.start(
                gui="edgechromium" if sys.platform == "win32" else "cocoa",
                icon=str(Path(__file__).with_name("web") / "app-icon.png"),
                private_mode=False,
                storage_path=str(app.preferences_path.parent / "webview"),
            )
        finally:
            server.shutdown()
            worker.join(timeout=5)


def smoke_test() -> dict:
    """Check a frozen bundle offline without accessing the user's library or login."""
    import webview  # noqa: F401
    from io import BytesIO
    from pypdf import PdfWriter
    import pypdfium2
    import certifi
    import ssl

    assert ssl.create_default_context(cafile=certifi.where()).cert_store_stats()["x509_ca"] > 0

    buffer = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(buffer)
    with pypdfium2.PdfDocument(buffer.getvalue()) as pdf:
        page = pdf[0]
        try:
            bitmap = page.render(scale=1)
            try:
                image = bitmap.to_pil()
                assert image.size == (72, 72)
                image.close()
            finally:
                bitmap.close()
        finally:
            page.close()
    with tempfile.TemporaryDirectory(prefix="az-desktop-smoke-") as temporary:
        app = LocalApp(preferences_path=Path(temporary) / "preferences.json")
        with LocalServer(("127.0.0.1", 0), app=app) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
            try:
                for route in ("/", "/styles.css", "/app.js", "/workspace.js", "/workspace.css",
                              "/workspace-vendor.js", "/workspace-index-worker.js", "/workspace-graph-worker.js",
                              "/workspace-licenses.txt"):
                    connection.request("GET", route)
                    response = connection.getresponse()
                    body = response.read()
                    assert response.status == 200 and body
                    assert b"__SESSION_TOKEN__" not in body
                connection.request("GET", "/api/state", headers={"X-Session-Token": app.token})
                response = connection.getresponse()
                state = json.loads(response.read())
                assert response.status == 200 and not state["auth"]["ready"]
                assert state["run"]["status"] == "idle"
            finally:
                connection.close()
                server.shutdown()
                worker.join(timeout=5)
    return {"status": "passed", "platform": sys.platform, "checks": ["engine_imports", "tls_certificates", "pdf_rendering", "web_assets", "local_session"]}


def _startup_error(message: str) -> None:
    if sys.platform == "darwin":
        subprocess.run(["osascript", "-e", f'display alert "Auto-Zettelkasten could not start" message {json.dumps(message)} as critical'], check=False)
    elif sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "Auto-Zettelkasten could not start", 0x10)
    elif sys.stderr is not None:
        print(message, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Auto-Zettelkasten desktop app")
    parser.add_argument("--smoke-test", action="store_true", help="Verify the bundle offline without opening a window.")
    parser.add_argument("--smoke-report", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.smoke_test:
            report = smoke_test()
            if args.smoke_report:
                args.smoke_report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            elif sys.stdout is not None:
                print(json.dumps(report))
        else:
            if sys.platform not in {"darwin", "win32"}:
                raise RuntimeError("The desktop app supports macOS and Windows. Use 'auto-zettelkasten serve' on other systems.")
            launch()
        return 0
    except Exception as exc:
        if args.smoke_test:
            if args.smoke_report:
                args.smoke_report.write_text(json.dumps({"status": "failed", "error": str(exc)}), encoding="utf-8")
            elif sys.stderr is not None:
                print(f"Desktop smoke test failed: {exc}", file=sys.stderr)
        else:
            _startup_error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
