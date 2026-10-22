"""Local, approval-gated UI adapter for the public mapping API."""
from __future__ import annotations

import copy
import json
import math
import os
import re
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
from typing import Any, Callable, Mapping

from . import api
from .documents import DocumentConflict, DocumentError, DocumentIndexChanged, DocumentService
from .files import read_yaml, slugify
from .models import DEFAULT_CODEX_SOURCE_MODEL, LiteratureMappingPolicy, MapRequest
from .readers import (ProviderError, _codex_environment, _codex_executable,
                      _attach_codex_process_job, _close_codex_process_job,
                      _redact_codex_diagnostic, codex_preflight_status)
from .zotero import ZoteroLocalClient, _api_collection_path

_POLICY = LiteratureMappingPolicy(cluster_generation_enabled=False)


class RequestError(ValueError):
    pass


class FrozenSource:
    """Keep approved source membership fixed while delegating attachment reads."""
    def __init__(self, client: Any, scope: str, key: str | None, items: list[dict], collections: list[dict], library_items: list[dict]):
        self.client, self.scope, self.key = client, scope, key
        self.items, self.rows, self.library_items = copy.deepcopy((items, collections, library_items))

    def inventory(self, scope: str, collection_key: str | None = None):
        if scope == self.scope and (collection_key or None) == self.key:
            return copy.deepcopy(self.items)
        if scope == "library" and not collection_key:
            return copy.deepcopy(self.library_items)
        raise RequestError("The requested inventory differs from the approved source. Review a new estimate.")

    def collections(self):
        return copy.deepcopy(self.rows)

    def __getattr__(self, name):
        return getattr(self.client, name)


def _source_key(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value))


def _collection_tree(rows):
    if not isinstance(rows, list) or len(rows) > 10000:
        raise RequestError("The Zotero collection hierarchy exceeds the supported inventory limit.")
    by_key, parents, identities = {}, {}, {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("data", row), dict):
            raise RequestError("Zotero returned an invalid collection. Refresh its inventory.")
        data = row.get("data", row)
        key = row.get("key") or data.get("key")
        parent = data.get("parentCollection")
        if parent is False or parent is None:
            parent = ""
        if (not _source_key(key) or (parent != "" and not _source_key(parent))
                or (data.get("key") and data["key"] != key)
                or not isinstance(data.get("name", ""), str)):
            raise RequestError("Zotero returned an invalid collection hierarchy. Refresh its inventory.")
        if key in by_key and (parents[key] != parent or by_key[key].get("data", by_key[key]).get("name") != data.get("name")):
            raise RequestError("Zotero returned conflicting collection identities. Refresh its inventory.")
        if key.casefold() in identities and identities[key.casefold()] != key:
            raise RequestError("Zotero returned conflicting collection identities. Refresh its inventory.")
        identities[key.casefold()] = key
        by_key[key], parents[key] = row, parent
    for key, parent in parents.items():
        if parent and parent not in by_key:
            raise RequestError("A Zotero parent collection is missing. Refresh its complete inventory.")
    depths = {}
    for key in by_key:
        path, seen, current = [], set(), key
        while current and current not in depths:
            if current in seen or len(path) >= 256:
                raise RequestError("The Zotero collection hierarchy contains a cycle or exceeds the depth limit.")
            path.append(current); seen.add(current)
            current = parents[current]
        depth = depths.get(current, 0)
        for child in reversed(path):
            depth += 1
            if depth > 256:
                raise RequestError("The Zotero collection hierarchy exceeds the depth limit.")
            depths[child] = depth
    return list(by_key.values()), by_key, parents


def _source_items(values):
    if not isinstance(values, list) or len(values) > 100000:
        raise RequestError("The Zotero source inventory exceeds the supported limit.")
    by_key = {}
    for row in values:
        if not isinstance(row, dict) or not isinstance(row.get("data", row), dict):
            raise RequestError("Zotero returned an invalid source item. Refresh its inventory.")
        data = row.get("data", row)
        key = row.get("key") or data.get("key")
        if not _source_key(key) or (data.get("key") and data["key"] != key):
            raise RequestError("Zotero returned an invalid source identity. Refresh its inventory.")
        canonical = key.casefold()
        if canonical in by_key and (by_key[canonical].get("data", by_key[canonical]) != data
                                   or by_key[canonical].get("version") != row.get("version")):
            raise RequestError("A Zotero item changed during inventory. Review a fresh estimate.")
        by_key.setdefault(canonical, row)
    return list(by_key.values())


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
    if kind not in {"folder", "vault", "codex", "embedding-model"}:
        raise RequestError("Unknown folder selection.")
    if sys.platform == "darwin":
        result = subprocess.run(
            ["osascript", "-e", 'POSIX path of (choose file with prompt "Choose a Codex executable")' if kind == "codex" else 'POSIX path of (choose folder with prompt "Choose a cached MiniLM model folder")' if kind == "embedding-model" else 'POSIX path of (choose folder with prompt "Choose a destination folder")'],
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
        self.view_zoom = None
        self.preferences_path = preferences_path or Path.home() / ".config" / "auto-zettelkasten" / "frontend.json"
        self.preferences = {"vault": "", "remember_vault": False}
        self.codex_executable = ""
        self.embedding_model_path = ""
        try:
            saved = json.loads(self.preferences_path.read_text())
            if saved.get("remember_vault") is True and isinstance(saved.get("vault"), str):
                self.preferences = {"vault": saved["vault"], "remember_vault": True}
            if isinstance(saved.get("codex_executable"), str):
                self.codex_executable = saved["codex_executable"]
            model_path = saved.get("embedding_model_path")
            if isinstance(model_path, str) and len(model_path) <= 4096 and "\x00" not in model_path:
                self.embedding_model_path = model_path
        except (OSError, ValueError, AttributeError):
            pass
        self.auth = {"method": "", "ready": False, "status": "disconnected", "message": "Choose a connection."}
        self.api_key = ""
        self.secret_values: set[str] = set()
        self.ticket: dict | None = None
        self.run: dict = {"status": "idle"}
        self.login_process = None
        self.documents = DocumentService(
            journal_root=self.preferences_path.parent / "workspace-operations",
            override_root=self.preferences_path.parent / "workspace-overrides",
        )

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
            if self.embedding_model_path:
                saved["embedding_model_path"] = self.embedding_model_path
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
            if method == "POST" and route.startswith("/api/workspace/"):
                return self._document_request(route, data)
            if method == "GET":
                if route == "/api/state":
                    return {"preferences": dict(self.preferences), "auth": dict(self.auth), "run": self._run_state(), "codex_executable": self.codex_executable, "embedding_model_path": self.embedding_model_path, "default_output_folder": str(Path.home() / "Documents" / "Auto-Zettelkasten Maps")}
                if route == "/api/run":
                    return self._run_state()
                if route == "/api/collections":
                    try:
                        rows, _, parents = _collection_tree(api.list_collections(client=self.client))
                    except RequestError:
                        raise
                    except Exception as exc:
                        raise RequestError("Zotero is unavailable. Open Zotero and enable its local API in Settings → Advanced.") from exc
                    return {"collections": [{"key": row.get("key") or row.get("data", {}).get("key", ""), "name": row.get("data", row).get("name", ""), "path": " / ".join(_api_collection_path(row, rows)), "parent_key": parents[row.get("key") or row["data"]["key"]]} for row in rows]}
                raise RequestError("Unknown endpoint.")
            if route == "/api/view-zoom":
                level = data.get("level")
                if type(level) not in (int, float) or not math.isfinite(level) or not .5 <= level <= 2:
                    raise RequestError("View zoom must be between 50% and 200%.")
                if self.view_zoom is None:
                    raise RequestError("View zoom is available in the desktop app.")
                try:
                    self.view_zoom(level)
                except RequestError:
                    raise
                except Exception as exc:
                    raise RequestError("The desktop view could not change size. Restart the app and try again.") from exc
                return {"level": level}
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
                if kind not in {"folder", "vault", "codex", "embedding-model"}:
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
                self.run = {"status": "running", "run_id": ticket["run_id"], "workspace": str(ticket["request"].workspace),
                            "memory_workspace": str(ticket["request"].workspace), "source": copy.deepcopy(ticket["public"]["source"]),
                            "obsidian_path": str(ticket["export"]) if ticket["export"] else "",
                            "message": "Mapping the approved Zotero source."}
                threading.Thread(target=self._map, args=(ticket,), daemon=True).start()
                return dict(self.run)
            raise RequestError("Unknown endpoint.")

    def _document_request(self, route: str, data: dict):
        try:
            if route == "/api/workspace/open":
                return {"workspace": self.documents.open(data.get("path"))}
            workspace_id = data.get("workspace_id")
            if route == "/api/workspace/tree":
                return {"workspace": self.documents.tree(workspace_id)}
            if route == "/api/workspace/index":
                if data.get("paged") is True:
                    return self.documents.index_page(workspace_id, cursor=data.get("cursor"), known_generation=data.get("known_generation"))
                return self.documents.index(workspace_id)
            if route == "/api/workspace/relocate-preview":
                self._editable()
                return self.documents.prepare_relocation(workspace_id, data.get("source"), data.get("destination"))
            if route == "/api/workspace/relocate":
                self._editable()
                return self.documents.apply_relocation(workspace_id, data.get("token"), data.get("repair"), data.get("patches"))
            if route == "/api/workspace/read":
                return {"document": self.documents.read(workspace_id, data.get("path"))}
            if route == "/api/workspace/save":
                self._editable()
                return {"document": self.documents.save(workspace_id, data.get("document_id"), data.get("revision"), data.get("text"))}
            if route == "/api/workspace/save-override":
                self._editable()
                return {"document": self.documents.save_override(workspace_id, data.get("document_id"), data.get("revision"), data.get("text"))}
            if route == "/api/workspace/create":
                self._editable()
                return self.documents.create(workspace_id, data.get("parent", ""), data.get("name"), data.get("kind"))
            raise RequestError("Unknown endpoint.")
        except (DocumentConflict, DocumentIndexChanged):
            raise
        except DocumentError as exc:
            raise RequestError(str(exc)) from exc

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
        scope = data.get("scope", "collection")
        descendants = data.get("include_subcollections", False)
        if not isinstance(scope, str) or scope not in {"library", "collection"}:
            raise RequestError("Choose the entire Zotero library or one collection.")
        if type(descendants) is not bool or ("remember_vault" in data and type(data["remember_vault"]) is not bool):
            raise RequestError("Subcollections and remember vault choices must be true or false.")
        key = data.get("collection_key")
        if scope == "library" and (key not in (None, "") or descendants):
            raise RequestError("Library scope cannot include a collection selection or subcollection option.")
        if scope == "collection" and not _source_key(key):
            raise RequestError("Choose a Zotero collection.")
        if str(getattr(self.client, "library_id", "0")) != "0":
            raise RequestError("This mapper currently supports the personal Zotero Desktop library only.")
        try:
            rows, by_key, parents = _collection_tree(api.list_collections(client=self.client))
            if scope == "collection" and key not in by_key:
                raise RequestError("Choose a Zotero collection from the current inventory.")
            library_items = copy.deepcopy(_source_items(list(self.client.inventory("library"))))
            if scope == "library":
                key, collection_keys = None, []
                items = library_items
                name = path = "Entire Zotero library"
                folder_name = "zotero-library"
            else:
                collection = by_key[key]
                name = collection.get("data", collection).get("name") or "Collection"
                path = " / ".join(_api_collection_path(collection, rows))
                collection_keys = [key]
                if descendants:
                    wanted = {key}
                    children = {}
                    for child, parent in parents.items():
                        children.setdefault(parent, []).append(child)
                    for parent in collection_keys:
                        for child in sorted(children.get(parent, [])):
                            if child not in wanted:
                                wanted.add(child); collection_keys.append(child)
                raw_items = []
                for collection_key in collection_keys:
                    raw_items.extend(_source_items(list(self.client.inventory("collection", collection_key))))
                    if len(raw_items) > 100000:
                        raise RequestError("The selected collections exceed the supported source inventory limit.")
                items = _source_items(raw_items)
                library_by_key = {str(row.get("key") or row.get("data", {}).get("key")).casefold(): row for row in library_items}
                for row in items:
                    frozen = library_by_key.get(str(row.get("key") or row.get("data", {}).get("key")).casefold())
                    if (frozen is None or frozen.get("data", frozen) != row.get("data", row)
                            or frozen.get("version") != row.get("version")):
                        raise RequestError("Zotero source metadata or membership changed during inventory. Review a fresh estimate.")
                folder_name = f"{slugify(str(name))[:80] or 'collection'}-{slugify(str(key))}"
        except RequestError:
            raise
        except Exception as exc:
            raise RequestError("Zotero inventory is unavailable. Open Zotero, enable its local API, and refresh the source list.") from exc
        source = {"scope": scope, "key": key or "", "name": name, "path": path,
                  "include_subcollections": descendants, "collection_keys": collection_keys, "item_count": len(items), "library_id": "0"}
        destination = data.get("destination", "folder")
        if not isinstance(destination, str) or destination not in {"folder", "obsidian"}:
            raise RequestError("Choose folder or Obsidian.")
        base = _directory(data.get("output_folder") or str(Path.home() / "Documents" / "Auto-Zettelkasten Maps"))
        workspace = base / folder_name
        self._check_workspace(workspace)
        vault = _directory(data.get("vault"), vault=True) if destination == "obsidian" else None
        run_id = f"web-{secrets.token_hex(8)}"
        export = vault / "Auto-Zettelkasten" / f"{folder_name}-{run_id}" if vault else None
        client = FrozenSource(self.client, scope, key, items, rows, library_items)
        if not client.items:
            raise RequestError("The selected Zotero source contains no documents to map.")
        provider = self.auth["method"]
        model = DEFAULT_CODEX_SOURCE_MODEL if provider == "codex" else "deepseek-v4-flash"
        mapping_config = read_yaml(workspace / "auto-zettelkasten.yml", {})
        if not isinstance(mapping_config, Mapping):
            raise RequestError("Workspace configuration must be a mapping.")
        mapping_backend = mapping_config.get("mapping_backend", "local")
        local_mapping = mapping_config.get("local_mapping", {})
        if not isinstance(local_mapping, Mapping):
            raise RequestError("Local mapping settings must be a mapping.")
        local_mapping = dict(local_mapping)
        selected_model = data.get("embedding_model_path")
        if selected_model is not None and (not isinstance(selected_model, str) or len(selected_model) > 4096 or "\x00" in selected_model):
            raise RequestError("Choose an existing MiniLM model folder.")
        selected_model = selected_model.strip() if selected_model is not None else None
        model_path = selected_model if selected_model is not None else (self.embedding_model_path if not local_mapping.get("model_path") else "")
        if mapping_backend == "local" and model_path:
            from .local_mapping import LocalMappingError
            from .mapping import check_local_mapping_availability

            try:
                local_mapping["model_path"] = str(Path(model_path).expanduser().resolve())
                check_local_mapping_availability(local_mapping)
            except (LocalMappingError, OSError, ValueError) as exc:
                raise RequestError(str(exc)) from exc
        request = MapRequest(workspace=workspace, scope=scope, collection_key=key, provider=provider, model=model, literature_model="gpt-5.6-terra" if provider == "codex" else None, reasoning_effort="medium" if provider == "codex" else None, allow_cloud=True, literature_policy=_POLICY,
                             mapping_backend=mapping_backend, local_mapping=local_mapping)
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
                for source_path in inputs:
                    if source_path.is_file():
                        target = scratch / source_path.relative_to(workspace)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(source_path, target)
            estimate = api.estimate_cost(scratch, scope=scope, collection_key=key or "", provider=provider, model=model, literature_policy=_POLICY, zotero_client=client, mapping_backend=request.mapping_backend, local_mapping=request.local_mapping)
            estimate.pop("path", None)
        public = {"estimate_id": secrets.token_urlsafe(24), "source": source, "collection": {"key": key or "", "name": name, "path": path}, "workspace": str(workspace), "memory_workspace": str(workspace), "obsidian_path": str(export) if export else "", "destination": destination, "estimate": estimate}
        public.update(mapping_backend=request.mapping_backend, embedding_model_path=request.local_mapping["model_path"] if request.mapping_backend == "local" else None)
        ticket = {"public": public, "request": request, "client": client, "created": time.monotonic(), "run_id": run_id, "export": export, "vault": vault, "api_key": self.api_key}
        self._check_export(ticket)
        if selected_model is not None and mapping_backend == "local":
            self.embedding_model_path = local_mapping["model_path"] if selected_model else ""
            self._write_preferences(self.preferences)
        if destination == "obsidian" and "remember_vault" in data:
            self._preferences({"vault": str(vault) if vault else "", "remember_vault": data["remember_vault"]})
        self.ticket = ticket
        return public

    def _run_state(self):
        result = self._redact(dict(self.run))
        workspace = result.get("workspace")
        if workspace:
            result["memory_workspace"] = workspace
            result["workspace_available"] = Path(workspace).is_dir() and (Path(workspace) / "auto-zettelkasten.yml").is_file()
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
            obsidian, export_error = None, ""
            if ticket["export"] and terminal != "failed":
                try:
                    self._check_export(ticket)
                    obsidian = api.export_to_obsidian(ticket["request"].workspace, ticket["vault"], project_folder=ticket["export"].name).to_dict()
                    if obsidian["status"] == "blocked":
                        terminal = "partial"
                except Exception:
                    terminal = "partial"
                    export_error = "The memory workspace was mapped, but export did not finish. Review the local export destination before retrying."
            outcome = dict(status=terminal, report=report, obsidian=obsidian, export_error=export_error,
                           message="Mapping finished." if terminal == "completed" else "Mapping needs review.")
        except Exception:
            outcome = dict(status="failed", error="Mapping or export failed. Review the local run files, connection, and destination before retrying.")
        finally:
            with self.lock:
                if ticket["request"].provider == "deepseek":
                    if previous_key is None:
                        os.environ.pop("DEEPSEEK_API_KEY", None)
                    else:
                        os.environ["DEEPSEEK_API_KEY"] = previous_key
                outcome["workspace"] = str(ticket["request"].workspace)
                outcome["memory_workspace"] = str(ticket["request"].workspace)
                outcome["source"] = copy.deepcopy(ticket["public"]["source"])
                outcome["workspace_available"] = ticket["request"].workspace.is_dir() and (ticket["request"].workspace / "auto-zettelkasten.yml").is_file()
                self.run.update(outcome)


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address=("127.0.0.1", 8765), *, app=None):
        if address[0] != "127.0.0.1":
            raise ValueError("The frontend must bind to loopback.")
        self.app = app or LocalApp()
        super().__init__(address, Handler)

    def server_close(self):
        try:
            super().server_close()
        finally:
            self.app.documents.close()


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
        self.send_header("Content-Security-Policy", f"default-src 'self'; style-src 'self' 'nonce-{self.server.app.token}'; script-src 'self'; connect-src 'self'; img-src 'none'; object-src 'none'; frame-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
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
                    limit = 6 * 1024 * 1024 + 16384 if route in {"/api/workspace/save", "/api/workspace/save-override", "/api/workspace/relocate"} else 16384
                    if not 0 < length <= limit:
                        raise RequestError("Request too large or empty.")
                    try:
                        data = json.loads(self.rfile.read(length))
                    except (ValueError, UnicodeError):
                        raise RequestError("Invalid JSON request.")
                    if not isinstance(data, dict):
                        raise RequestError("Expected a JSON object.")
                self._reply(200, self.server.app.handle(self.command, route, data))
                return
            assets = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/styles.css": ("styles.css", "text/css; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/workspace.js": ("workspace.js", "text/javascript; charset=utf-8"),
                "/workspace.css": ("workspace.css", "text/css; charset=utf-8"),
                "/workspace-vendor.js": ("workspace-vendor.js", "text/javascript; charset=utf-8"),
                "/workspace-index-worker.js": ("workspace-index-worker.js", "text/javascript; charset=utf-8"),
                "/workspace-graph-worker.js": ("workspace-graph-worker.js", "text/javascript; charset=utf-8"),
                "/workspace-licenses.txt": ("workspace-licenses.txt", "text/plain; charset=utf-8"),
            }
            if self.command != "GET" or route not in assets:
                self._reply(404, {"error": "Not found."})
                return
            filename, content_type = assets[route]
            body = (Path(__file__).parent / "web" / filename).read_bytes()
            if filename == "index.html":
                body = body.replace(b"__SESSION_TOKEN__", self.server.app.token.encode())
                body = body.replace(b"__DESKTOP_SHELL__", b"true" if self.server.app.view_zoom else b"false")
            self._reply(200, body, content_type)
        except DocumentConflict as exc:
            self._reply(409, {"error": str(exc), "code": "conflict", "current": exc.current})
        except DocumentIndexChanged as exc:
            self._reply(409, {"error": str(exc), "code": "index_changed"})
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
