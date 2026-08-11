from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
import tomllib

import pytest

from auto_zettelkasten.api import (
    doctor,
    export_to_obsidian,
    initialize_workspace,
    inventory,
    run_literature_map,
)
from auto_zettelkasten.cli import main
from auto_zettelkasten.files import read_yaml, write_yaml
from auto_zettelkasten.models import LiteratureMapRequest
from auto_zettelkasten.obsidian import _missing_links_from_contents

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
    assert result["artifact_manifest"]["artifact_schema_version"] == "1.20"




def test_release_metadata_matches_engine_version() -> None:
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    payload = tomllib.loads(pyproject.read_text(encoding="utf-8"))

    assert payload["project"]["version"] == "0.29.10"


def test_standalone_literature_map_forwards_provider_concurrency(
    tmp_path: Path, monkeypatch
) -> None:
    captured: dict[str, object] = {}

    def fake_build_map(*_args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            status="completed",
            run_id="concurrency-run",
            metadata={},
            artifacts=[],
        )

    monkeypatch.setattr("auto_zettelkasten.api.build_map", fake_build_map)
    run_literature_map(
        LiteratureMapRequest(
            tmp_path,
            run_id="concurrency-run",
            provider_concurrency=17,
        )
    )

    assert captured["provider_concurrency"] == 17


def test_build_map_cli_prints_compact_receipt(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    initialize_workspace(tmp_path)

    class Manifest:
        def to_dict(self):
            return {
                "status": "built",
                "workspace": str(tmp_path),
                "run_id": "compact",
                "artifacts": [{"path": "a"}, {"path": "b"}],
                "metadata": {
                    "literature_map": {
                        "cluster_count": 2,
                        "large_payload": {"rows": list(range(100))},
                    }
                },
            }

    monkeypatch.setattr("auto_zettelkasten.cli.build_map", lambda *_a, **_k: Manifest())

    assert main(["build-map", "--workspace", str(tmp_path), "--no-synthesis"]) == 0
    result = json.loads(capsys.readouterr().out)

    assert "artifacts" not in result
    assert result["artifact_count"] == 2
    assert result["metadata"] == {"literature_map": {"cluster_count": 2}}


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


def test_obsidian_export_of_canonical_literature_map_has_no_missing_wikilinks(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    initialize_workspace(workspace)
    source_note = workspace / "02_source_memory" / "notes" / "Source A.md"
    cluster_root = workspace / "03_literature_synthesis" / "maps" / "map-v5" / "clusters"
    gap_root = cluster_root.parent / "gaps"
    source_note.write_text("# Source A\n\nRelated: [[Cluster A]]\n", encoding="utf-8")
    cluster_root.mkdir(parents=True)
    gap_root.mkdir()
    (cluster_root / "Cluster A.md").write_text(
        "# Cluster A\n\nSource: [[Source A]]\n\nGap: [[Gap A]]\n",
        encoding="utf-8",
    )
    (cluster_root / "INDEX.md").write_text(
        "# Cluster Index\n\n- [[Cluster A]]\n", encoding="utf-8"
    )
    (gap_root / "Gap A.md").write_text(
        "# Gap A\n\nCluster: [[Cluster A]]\n", encoding="utf-8"
    )
    (gap_root / "INDEX.md").write_text(
        "# Gap Index\n\n- [[Gap A]]\n", encoding="utf-8"
    )
    map_path = cluster_root.parent / "Literature Map - Mediation.md"
    map_path.write_text(
        "# Literature Map - Mediation\n\n"
        "- [[clusters/INDEX|Cluster Index]]\n"
        "- [[gaps/INDEX|Gap Index]]\n"
        "- [[02_source_memory/indexes/INDEX|Source Index]]\n",
        encoding="utf-8",
    )
    write_yaml(
        cluster_root.parent / "manifest.yml",
        {
            "updated_at": "2026-07-21T00:00:00Z",
            "artifacts": {"literature_map_markdown": str(map_path)},
        },
    )

    result = export_to_obsidian(
        workspace,
        tmp_path / "vault",
        dry_run=True,
        new_vault=True,
    )

    assert result.status == "dry_run"
    assert result.metadata["missing_wikilink_count"] == 0
    assert result.metadata["missing_wikilinks"] == []
    assert all(
        "Literature Neighborhoods" not in path
        for path in result.metadata["planned_files"]
    )


def test_obsidian_export_omits_cluster_files_not_in_current_registry(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    initialize_workspace(workspace)
    map_root = workspace / "03_literature_synthesis" / "maps" / "map-v5"
    cluster_root = map_root / "clusters"
    cluster_root.mkdir(parents=True)
    current = {
        "cluster_id": "current",
        "label": "Current",
    }
    current_name = "Cluster - Current [current].md"
    (cluster_root / current_name).write_text("# Current\n", encoding="utf-8")
    (cluster_root / "Cluster - Stale [stale].md").write_text(
        "<!-- auto-zettelkasten:cluster:start -->\n# Stale\n",
        encoding="utf-8",
    )
    write_yaml(map_root / "cluster_registry.yml", {"clusters": [current]})
    write_yaml(
        map_root / "manifest.yml",
        {"updated_at": "2026-07-21T00:00:00Z", "artifacts": {}},
    )

    result = export_to_obsidian(
        workspace,
        tmp_path / "vault",
        dry_run=True,
        new_vault=True,
    )

    planned = set(result.metadata["planned_files"])
    assert f"Clusters/{current_name}" in planned
    assert "Clusters/Cluster - Stale [stale].md" not in planned


def test_obsidian_missing_link_scan_ignores_yaml_frontmatter(tmp_path: Path) -> None:
    export_root = tmp_path / "vault"
    source = export_root / "Sources" / "What's in a Figure.md"
    contents = {
        source: (
            "---\n"
            "related_notes:\n"
            "  - wikilink: '[[What''s in a Figure]]'\n"
            "---\n"
            "# What's in a Figure\n\n"
            "See [[Cluster A]] and [[Actually Missing]].\n"
        ),
        export_root / "Clusters" / "Cluster A.md": "# Cluster A\n",
    }

    assert _missing_links_from_contents(export_root, contents) == [
        {"source": "Sources/What's in a Figure.md", "target": "Actually Missing"}
    ]


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
