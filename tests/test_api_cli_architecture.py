from __future__ import annotations

import ast
import json
from pathlib import Path
import tomllib

import pytest

from auto_zettelkasten.api import doctor, export_to_obsidian, initialize_workspace, inventory
from auto_zettelkasten.cli import main
from auto_zettelkasten.files import read_yaml, write_yaml

from conftest import FakeZotero


def test_package_has_no_research_os_imports() -> None:
    package = Path(__file__).parents[1] / "src" / "auto_zettelkasten"
    violations = []
    for path in package.glob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(alias.name == "research_os" or alias.name.startswith("research_os.") for alias in node.names):
                violations.append(path.name)
            if isinstance(node, ast.ImportFrom) and node.module and (node.module == "research_os" or node.module.startswith("research_os.")):
                violations.append(path.name)
    assert violations == []


def test_inventory_only_writes_scope_manifest_and_resolves_selected(tmp_path: Path, sample_items) -> None:
    client = FakeZotero(sample_items, selected_key="SELECTEDKEY")
    result = inventory(tmp_path, "inventory-run", "selected", limit=1, zotero_client=client)
    assert result["status"] == "inventoried"
    assert result["item_count"] == 1
    assert result["collection_key"] == "SELECTEDKEY"
    assert result["source_set"]["zotero_collection_key"] == "SELECTEDKEY"
    assert result["artifact_manifest"]["artifact_schema_version"] == "1.7"


def test_release_metadata_is_0_8_0() -> None:
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    payload = tomllib.loads(pyproject.read_text(encoding="utf-8"))

    assert payload["project"]["version"] == "0.8.0"


def test_run_ids_cannot_escape_workspace_state(tmp_path: Path, sample_items) -> None:
    workspace = tmp_path / "workspace"
    with pytest.raises(ValueError, match="opaque"):
        inventory(workspace, "../../escape", zotero_client=FakeZotero(sample_items))
    assert not (tmp_path / "escape").exists()


def test_doctor_reports_each_required_surface(tmp_path: Path, sample_items) -> None:
    initialize_workspace(tmp_path)
    report = doctor(tmp_path, client=FakeZotero(sample_items))
    assert report.status == "blocked"
    assert set(report.checks) == {"workspace", "schema", "zotero", "provider", "pdf_extraction", "ocr", "privacy", "obsidian"}
    assert report.checks["zotero"]["read_only"] is True
    assert report.checks["privacy"]["allow_cloud"] is False
    config = read_yaml(tmp_path / "auto-zettelkasten.yml")
    config.update(provider="ollama", model="llama3.2")
    write_yaml(tmp_path / "auto-zettelkasten.yml", config)
    assert doctor(tmp_path, client=FakeZotero(sample_items)).status == "ready"


def test_obsidian_dry_run_does_not_create_vault(tmp_path: Path) -> None:
    initialize_workspace(tmp_path / "workspace")
    vault = tmp_path / "missing-vault"
    result = export_to_obsidian(tmp_path / "workspace", vault, dry_run=True, new_vault=True)
    assert result.status == "dry_run"
    assert not vault.exists()
    assert result.metadata["canonical_state_edited"] is False
    assert result.metadata["missing_wikilink_count"] == 0
    assert result.metadata["file_count"] == 5


def test_obsidian_replace_rejects_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    initialize_workspace(workspace)
    vault = tmp_path / "vault"
    outside = tmp_path / "outside"
    vault.mkdir()
    outside.mkdir()
    keep = outside / workspace.name / "KEEP.txt"
    keep.parent.mkdir()
    keep.write_text("keep")
    (vault / "Auto-Zettelkasten").symlink_to(outside, target_is_directory=True)
    result = export_to_obsidian(workspace, vault, replace=True)
    assert result.status == "blocked"
    assert result.metadata["reason"] == "unsafe_export_target"
    assert keep.read_text() == "keep"


def test_cli_init_status_and_collections_smoke(tmp_path: Path, monkeypatch, capsys) -> None:
    workspace = tmp_path / "cli-workspace"
    assert main(["init", str(workspace)]) == 0
    init_payload = json.loads(capsys.readouterr().out)
    assert init_payload["status"] == "initialized"
    assert main(["status", "--workspace", str(workspace), "--json"]) == 0
    status_payload = json.loads(capsys.readouterr().out)
    assert status_payload["status"] == "initialized"
    monkeypatch.setattr("auto_zettelkasten.cli.list_collections", lambda: [{"key": "C1"}])
    assert main(["zotero", "collections"]) == 0
    collection_payload = json.loads(capsys.readouterr().out)
    assert collection_payload["collections"] == [{"key": "C1"}]


def test_cli_migrate_dry_run_reports_1_5_plan_without_mutation(tmp_path: Path, capsys) -> None:
    workspace = tmp_path / "migration-workspace"
    initialize_workspace(workspace)
    for path in (
        workspace / "auto-zettelkasten.yml",
        workspace / "11_state" / "workspace_manifest.yml",
    ):
        payload = read_yaml(path)
        payload.update(engine_version="0.5.0", artifact_schema_version="1.4")
        write_yaml(path, payload)
    before = {
        path: path.read_bytes()
        for path in (
            workspace / "auto-zettelkasten.yml",
            workspace / "11_state" / "workspace_manifest.yml",
        )
    }

    assert main(["migrate", "--workspace", str(workspace), "--dry-run"]) == 0
    result = json.loads(capsys.readouterr().out)

    assert result["status"] == "dry_run"
    assert result["provider_calls"] == 0
    assert result["proposition_anchors"]["status"] == "dry_run"
    assert all(path.read_bytes() == content for path, content in before.items())


def test_cli_uses_workspace_provider_but_requires_per_run_cloud_consent(tmp_path: Path, monkeypatch, capsys) -> None:
    workspace = tmp_path / "config-workspace"
    initialize_workspace(workspace)
    config = read_yaml(workspace / "auto-zettelkasten.yml")
    config.update(provider="deepseek", model="deepseek-v4-flash")
    config["privacy"]["allow_cloud"] = True
    write_yaml(workspace / "auto-zettelkasten.yml", config)
    captured = {}

    class Result:
        def to_dict(self):
            return {"status": "completed"}

    def fake_run(request, **kwargs):
        captured["request"] = request
        return Result()

    monkeypatch.setattr("auto_zettelkasten.cli.run_map", fake_run)
    assert main(["map", "--workspace", str(workspace)]) == 0
    capsys.readouterr()
    assert captured["request"].provider == "deepseek"
    assert captured["request"].allow_cloud is False


def test_cli_processing_overrides_and_partial_exit_code(tmp_path: Path, monkeypatch, capsys) -> None:
    workspace = tmp_path / "processing-workspace"
    initialize_workspace(workspace)
    captured = {}

    class Result:
        def to_dict(self):
            return {"status": "partial"}

    def fake_run(request, **kwargs):
        captured["request"] = request
        return Result()

    monkeypatch.setattr("auto_zettelkasten.cli.run_map", fake_run)
    exit_code = main(
        [
            "map",
            "--workspace",
            str(workspace),
            "--provider",
            "ollama",
            "--max-document-calls",
            "9",
            "--request-deadline-seconds",
            "45",
        ]
    )
    assert exit_code == 3
    capsys.readouterr()
    assert captured["request"].processing.max_calls_per_document_run == 9
    assert captured["request"].processing.request_deadline_seconds == 45
