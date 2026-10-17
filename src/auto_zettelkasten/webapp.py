"""Local, approval-gated UI adapter for the public mapping API."""
from __future__ import annotations

import copy
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from . import api
from .files import slugify
from .models import DEFAULT_CODEX_SOURCE_MODEL, LiteratureMappingPolicy, MapRequest
from .readers import (ProviderError, _codex_environment, _codex_executable,
                      _attach_codex_process_job, _close_codex_process_job,
                      _redact_codex_diagnostic, codex_preflight_status)
from .zotero import ZoteroLocalClient, _api_collection_path

_POLICY = LiteratureMappingPolicy(cluster_generation_enabled=False)


class RequestError(ValueError):
    pass


class FrozenCollection:
    """Keep approved source membership fixed while delegating attachment reads."""
    def __init__(self, client: Any, key: str, items: list[dict], collections: list[dict]):
        self.client, self.key = client, key
        self.items, self.rows = copy.deepcopy(items), copy.deepcopy(collections)

    def inventory(self, scope: str, collection_key: str | None = None):
        if scope == "collection" and collection_key == self.key:
            return copy.deepcopy(self.items)
        return self.client.inventory(scope, collection_key)

    def collections(self):
        return copy.deepcopy(self.rows)

    def __getattr__(self, name):
        return getattr(self.client, name)


def _directory(value: Any, *, vault: bool = False) -> Path:
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise RequestError("Choose a valid folder.")
    path = Path(value).expanduser().resolve()
    if vault:
        if not path.is_dir() or not (path / ".obsidian").is_dir():
            raise RequestError("Choose an existing Obsidian vault containing a .obsidian folder.")
    elif path.exists() and not path.is_dir():
        raise RequestError("The output location must be a folder.")
    return path


def pick_folder(kind: str) -> str:
    if kind not in {"folder", "vault", "codex"}:
        raise RequestError("Unknown folder selection.")
    if sys.platform == "darwin":
        result = subprocess.run(
            ["osascript", "-e", 'POSIX path of (choose file with prompt "Choose a Codex executable")' if kind == "codex" else 'POSIX path of (choose folder with prompt "Choose a destination folder")'],
            capture_output=True, text=True, timeout=180, check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    # Browser path fields remain available on systems without a native picker.
    return ""


class LocalApp:
    def __init__(self, *, preferences_path: Path | None = None, client: Any = None, folder_picker: Callable[[str], str] | None = None):
        self.lock = threading.RLock()
        self.token = secrets.token_urlsafe(32)
        self.client = client or ZoteroLocalClient()
        self.folder_picker = folder_picker or pick_folder
        self.preferences_path = preferences_path or Path.home() / ".config" / "auto-zettelkasten" / "frontend.json"
        self.preferences = {"vault": "", "remember_vault": False}
        self.codex_executable = ""
        try:
            saved = json.loads(self.preferences_path.read_text())
            if saved.get("remember_vault") is True and isinstance(saved.get("vault"), str):
                self.preferences = {"vault": saved["vault"], "remember_vault": True}
            if isinstance(saved.get("codex_executable"), str):
                self.codex_executable = saved["codex_executable"]
        except (OSError, ValueError, AttributeError):
            pass
        self.auth = {"method": "", "ready": False, "status": "disconnected", "message": "Choose a connection."}
        self.api_key = ""
        self.secret_values: set[str] = set()
        self.ticket: dict | None = None
        self.run: dict = {"status": "idle"}
        self.login_process = None

    def _editable(self):
        if self.run["status"] == "running" or self.auth["status"] == "pending":
            raise RequestError("Wait for the current operation to finish.")

    def _preferences(self, data):
        remember = data.get("remember_vault", False)
        if not isinstance(remember, bool):
            raise RequestError("Remember vault must be true or false.")
        vault = str(_directory(data.get("vault"), vault=True)) if remember else ""
        preferences = {"vault": vault, "remember_vault": remember}
        self._write_preferences(preferences)
        self.preferences = preferences
        return {"preferences": self.preferences}

    def _write_preferences(self, preferences):
        self.preferences_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=self.preferences_path.parent, delete=False) as handle:
            saved = dict(preferences)
            if self.codex_executable:
                saved["codex_executable"] = self.codex_executable
            json.dump(saved, handle)
            temporary = Path(handle.name)
        try:
            temporary.replace(self.preferences_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _configure_codex(self, data):
        value = data.get("codex_executable", self.codex_executable)
        if not isinstance(value, str) or len(value) > 4096:
            raise RequestError("Choose a compatible Codex executable.")
        if value.strip():
            environment = _codex_environment()
            environment["AUTO_ZETTELKASTEN_CODEX"] = value.strip()
            try:
                executable = _codex_executable(environment)
            except ProviderError as exc:
                raise RequestError(self._redact(str(exc))) from exc
            self.codex_executable = str(executable)
            os.environ["AUTO_ZETTELKASTEN_CODEX"] = self.codex_executable
            self._write_preferences(self.preferences)
        elif self.codex_executable:
            if os.environ.get("AUTO_ZETTELKASTEN_CODEX") == self.codex_executable:
                os.environ.pop("AUTO_ZETTELKASTEN_CODEX", None)
            self.codex_executable = ""
            self._write_preferences(self.preferences)

    def _codex_check(self):
        try:
            codex_preflight_status(DEFAULT_CODEX_SOURCE_MODEL, "medium", ("gpt-5.6-terra",))
            self.auth = {"method": "codex", "ready": True, "status": "connected", "message": "ChatGPT account connected through Codex."}
        except Exception as exc:
            reason = self._redact(_redact_codex_diagnostic(str(exc)))[:800] if isinstance(exc, ProviderError) else "The local Codex connection check failed."
            self.auth = {"method": "codex", "ready": False, "status": "unavailable", "message": f"{reason} Install a compatible Codex CLI or sign in, then check the connection."}

    def _login(self):
        process = None
        try:
            environment = _codex_environment()
            executable = str(_codex_executable(environment))
            process = subprocess.Popen([executable, "login"], env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) | 0x4) if sys.platform == "win32" else 0)
            _attach_codex_process_job(process)
            with self.lock:
                self.login_process = process
            try:
                code = process.wait(timeout=300)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
                code = -1
            with self.lock:
                if code == 0:
                    self._codex_check()
                else:
                    self.auth = {"method": "codex", "ready": False, "status": "unavailable", "message": "Sign-in did not complete. Retry or use the Codex CLI to sign in."}
        except Exception:
            with self.lock:
                self.auth = {"method": "codex", "ready": False, "status": "unavailable", "message": "Unable to start Codex login. Install the compatible Codex CLI first."}
        finally:
            if process is not None:
                _close_codex_process_job(process)

    def handle(self, method: str, route: str, data: dict | None = None):
        data = data or {}
        with self.lock:
            if method == "GET":
                if route == "/api/state":
                    return {"preferences": dict(self.preferences), "auth": dict(self.auth), "run": self._run_state(), "codex_executable": self.codex_executable, "default_output_folder": str(Path.home() / "Documents" / "Auto-Zettelkasten Maps")}
                if route == "/api/run":
                    return self._run_state()
                if route == "/api/collections":
                    try:
                        rows = api.list_collections(client=self.client)
                    except Exception as exc:
                        raise RequestError("Zotero is unavailable. Open Zotero and enable its local API in Settings → Advanced.") from exc
                    return {"collections": [{"key": row.get("key", ""), "name": (row.get("data") or {}).get("name", row.get("name", "")), "path": " / ".join(_api_collection_path(row, rows))} for row in rows]}
                raise RequestError("Unknown endpoint.")
            self._editable()
            if route == "/api/connect":
                self.ticket = None
                method_name = data.get("method")
                if method_name == "deepseek":
                    key = data.get("api_key") or os.getenv("DEEPSEEK_API_KEY", "")
                    if not isinstance(key, str) or not key.strip() or len(key) > 4096 or any(c.isspace() for c in key):
                        raise RequestError("Enter a DeepSeek API key.")
                    self.api_key = key
                    self.secret_values.add(key)
                    self.auth = {"method": "deepseek", "ready": True, "status": "configured", "message": "API key held in memory. Validity is checked when mapping starts."}
                elif method_name == "codex":
                    self.api_key = ""
                    self._configure_codex(data)
                    self._codex_check()
                else:
                    raise RequestError("Choose DeepSeek API key or ChatGPT account.")
                return {"auth": dict(self.auth)}
            if route == "/api/login":
                self.ticket = None
                self._configure_codex(data)
                self.auth = {"method": "codex", "ready": False, "status": "pending", "message": "Complete ChatGPT sign-in in the browser opened by Codex."}
                threading.Thread(target=self._login, daemon=True).start()
                return {"auth": dict(self.auth)}
            if route == "/api/pick-folder":
                kind = data.get("kind", "folder")
                if kind not in {"folder", "vault", "codex"}:
                    raise RequestError("Unknown folder selection.")
                return {"path": self.folder_picker(kind)}
            if route == "/api/preferences":
                self.ticket = None
                return self._preferences(data)
            if route == "/api/estimate":
                return self._estimate(data)
            if route in {"/api/approve", "/api/cancel"}:
                ticket = self.ticket
                if not ticket or data.get("estimate_id") != ticket["public"]["estimate_id"] or time.monotonic() - ticket["created"] > 1800:
                    raise RequestError("This estimate expired or changed. Review a new estimate.")
                if route == "/api/cancel":
                    self.ticket = None
                    return {"status": "cancelled"}
                if data.get("allow_cloud") is not True:
                    raise RequestError("Approval must explicitly allow sending source text to the chosen provider.")
                self._check_workspace(ticket["request"].workspace)
                self._check_export(ticket)
                self.ticket = None
                self.run = {"status": "running", "run_id": ticket["run_id"], "workspace": str(ticket["request"].workspace), "message": "Mapping the approved collection."}
                threading.Thread(target=self._map, args=(ticket,), daemon=True).start()
                return dict(self.run)
            raise RequestError("Unknown endpoint.")

    @staticmethod
    def _check_workspace(workspace):
        root = Path(workspace)
        if root.exists() and any(root.iterdir()) and not (root / "auto-zettelkasten.yml").is_file():
            raise RequestError("The destination contains unrelated files. Choose another output folder.")

    @staticmethod
    def _check_export(ticket):
        path = ticket.get("export")
        if path:
            if path.resolve() != path or path.exists():
                raise RequestError("The Obsidian destination changed or already exists. Review a new estimate.")
            _directory(str(ticket["vault"]), vault=True)

    def _estimate(self, data):
        self.ticket = None
        if not self.auth["ready"]:
            raise RequestError("Connect a provider first.")
        key = data.get("collection_key")
        rows = api.list_collections(client=self.client)
        collection = next((row for row in rows if row.get("key") == key), None)
        if not collection:
            raise RequestError("Choose a Zotero collection.")
        name = (collection.get("data") or {}).get("name", collection.get("name", "Collection"))
        folder_name = f"{slugify(str(name))[:80] or 'collection'}-{slugify(str(key))}"
        destination = data.get("destination", "folder")
        if destination not in {"folder", "obsidian"}:
            raise RequestError("Choose folder or Obsidian.")
        base = _directory(data.get("output_folder") or str(Path.home() / "Documents" / "Auto-Zettelkasten Maps"))
        workspace = base / folder_name
        self._check_workspace(workspace)
        vault = _directory(data.get("vault"), vault=True) if destination == "obsidian" else None
        run_id = f"web-{secrets.token_hex(8)}"
        export = vault / "Auto-Zettelkasten" / f"{folder_name}-{run_id}" if vault else None
        client = FrozenCollection(self.client, key, list(self.client.inventory("collection", key)), rows)
        if not client.items:
            raise RequestError("This collection contains no documents to map.")
        provider = self.auth["method"]
        model = DEFAULT_CODEX_SOURCE_MODEL if provider == "codex" else "deepseek-v4-flash"
        request = MapRequest(workspace=workspace, scope="collection", collection_key=key, provider=provider, model=model, literature_model="gpt-5.6-terra" if provider == "codex" else None, reasoning_effort="medium" if provider == "codex" else None, allow_cloud=True, literature_policy=_POLICY)
        with tempfile.TemporaryDirectory(prefix="az-estimate-") as temporary:
            scratch = Path(temporary)
            if workspace.exists():
                # Copy estimator inputs only: ordinary Windows users cannot create symlinks.
                inputs = [workspace / "auto-zettelkasten.yml", workspace / "03_literature_synthesis" / "cluster_registry.yml"]
                for relative, pattern in [
                    ("02_source_memory/notes", "*.md"),
                    ("02_source_memory/profiles", "*.yml"),
                    ("02_source_memory/indexes", "*.yml"),
                    ("11_state/runs", "*/literature/synthesis/cluster_synthesis/*.yml"),
                ]:
                    inputs.extend((workspace / relative).glob(pattern))
                for source in inputs:
                    if source.is_file():
                        target = scratch / source.relative_to(workspace)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(source, target)
            estimate = api.estimate_cost(scratch, scope="collection", collection_key=key, provider=provider, model=model, literature_policy=_POLICY, zotero_client=client)
            estimate.pop("path", None)
        public = {"estimate_id": secrets.token_urlsafe(24), "collection": {"key": key, "name": name, "path": " / ".join(_api_collection_path(collection, rows))}, "workspace": str(workspace), "obsidian_path": str(export) if export else "", "destination": destination, "estimate": estimate}
        ticket = {"public": public, "request": request, "client": client, "created": time.monotonic(), "run_id": run_id, "export": export, "vault": vault, "api_key": self.api_key}
        self._check_export(ticket)
        if destination == "obsidian" and "remember_vault" in data:
            self._preferences({"vault": str(vault) if vault else "", "remember_vault": data["remember_vault"]})
        self.ticket = ticket
        return public

    def _run_state(self):
        result = self._redact(dict(self.run))
        if result["status"] == "running":
            try:
                status = api.get_status(result["workspace"], result["run_id"]).to_dict()
                result["counts"] = status.get("counts", {})
            except Exception:
                pass
        return result

    def _redact(self, value):
        if isinstance(value, str):
            for secret in self.secret_values:
                value = value.replace(secret, "[redacted]")
            return value
        if isinstance(value, dict):
            return {key: self._redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        return value

    def _map(self, ticket):
        previous_key = os.environ.get("DEEPSEEK_API_KEY")
        outcome = {}
        try:
            if ticket["request"].provider == "deepseek":
                os.environ["DEEPSEEK_API_KEY"] = ticket["api_key"]
            report = api.run_map(ticket["request"], client=ticket["client"], run_id=ticket["run_id"]).to_dict()
            terminal = "failed" if report["status"] in {"blocked", "failed"} else "partial" if report.get("errors") or report.get("pending_count") or report.get("partial_count") or report.get("parked_for_review_count") or report.get("literature_failure_count") or report["status"] == "partial" else "completed"
            obsidian = None
            if ticket["export"] and terminal != "failed":
                self._check_export(ticket)
                obsidian = api.export_to_obsidian(ticket["request"].workspace, ticket["vault"], project_folder=ticket["export"].name).to_dict()
                if obsidian["status"] == "blocked":
                    terminal = "partial"
            outcome = dict(status=terminal, report=report, obsidian=obsidian, message="Mapping finished." if terminal == "completed" else "Mapping needs review.")
        except Exception:
            outcome = dict(status="failed", error="Mapping or export failed. Review the local run files, connection, and destination before retrying.")
        finally:
            with self.lock:
                if ticket["request"].provider == "deepseek":
                    if previous_key is None:
                        os.environ.pop("DEEPSEEK_API_KEY", None)
                    else:
                        os.environ["DEEPSEEK_API_KEY"] = previous_key
                self.run.update(outcome)


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address=("127.0.0.1", 8765), *, app=None):
        if address[0] != "127.0.0.1":
            raise ValueError("The frontend must bind to loopback.")
        self.app = app or LocalApp()
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, code, payload, content_type="application/json"):
        body = json.dumps(payload).encode() if content_type == "application/json" else payload
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def _handle(self):
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        origins = {f"http://{host}" for host in hosts}
        if self.headers.get("Host") not in hosts or (self.headers.get("Origin") is not None and self.headers["Origin"] not in origins) or self.headers.get("Sec-Fetch-Site") == "cross-site":
            self._reply(403, {"error": "Local access only."})
            return
        route = self.path.split("?", 1)[0]
        try:
            if route.startswith("/api/"):
                if not secrets.compare_digest(self.headers.get("X-Session-Token", ""), self.server.app.token):
                    self._reply(403, {"error": "Invalid local session."})
                    return
                data = None
                if self.command == "POST":
                    if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                        raise RequestError("Use JSON requests.")
                    try:
                        length = int(self.headers.get("Content-Length", "0"))
                    except ValueError:
                        raise RequestError("Invalid request length.")
                    if not 0 < length <= 16384:
                        raise RequestError("Request too large or empty.")
                    try:
                        data = json.loads(self.rfile.read(length))
                    except (ValueError, UnicodeError):
                        raise RequestError("Invalid JSON request.")
                    if not isinstance(data, dict):
                        raise RequestError("Expected a JSON object.")
                self._reply(200, self.server.app.handle(self.command, route, data))
                return
            assets = {"/": ("index.html", "text/html; charset=utf-8"), "/styles.css": ("styles.css", "text/css; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8")}
            if self.command != "GET" or route not in assets:
                self._reply(404, {"error": "Not found."})
                return
            filename, content_type = assets[route]
            body = (Path(__file__).parent / "web" / filename).read_bytes()
            if filename == "index.html":
                body = body.replace(b"__SESSION_TOKEN__", self.server.app.token.encode())
            self._reply(200, body, content_type)
        except RequestError as exc:
            self._reply(400, {"error": str(exc)})
        except Exception:
            self._reply(500, {"error": "The local operation failed. Check Zotero, your connection, and destination."})

    do_GET = _handle
    do_POST = _handle


def serve(*, port: int = 8765, open_browser: bool = True):
    with LocalServer(("127.0.0.1", port)) as server:
        url = f"http://127.0.0.1:{server.server_port}"
        print(f"Auto-Zettelkasten frontend: {url}")
        if open_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
