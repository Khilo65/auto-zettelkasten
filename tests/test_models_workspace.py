from __future__ import annotations

from pathlib import Path

import pytest

from auto_zettelkasten.files import read_yaml, write_yaml
from auto_zettelkasten.models import LiteratureMappingPolicy, MapRequest, ProcessingPolicy
from auto_zettelkasten.workspace import IncompatibleArtifactSchemaError, assert_compatible, initialize, load_config


def test_map_request_is_versioned_serializable_and_validated(tmp_path: Path) -> None:
    request = MapRequest(
        tmp_path,
        scope="collection",
        collection_key="C1",
        limit=3,
        processing=ProcessingPolicy(max_calls_per_document_run=7),
    )
    assert MapRequest.from_dict(request.to_dict()) == request
    assert request.processing.max_calls_per_document_run == 7
    assert request.prompt_version == "11"
    assert request.extraction_version == "2"
    assert request.extraction_policy.ocr == "auto"
    with pytest.raises(ValueError, match="collection_key"):
        MapRequest(tmp_path, scope="collection")
    with pytest.raises(ValueError, match="limit"):
        MapRequest(tmp_path, limit=-1)
    assert MapRequest.from_dict({"workspace": str(tmp_path), "allow_cloud": "false"}).allow_cloud is False
    with pytest.raises(ValueError, match="allow_cloud must be a boolean"):
        MapRequest(tmp_path, allow_cloud="false")  # type: ignore[arg-type]
    assert MapRequest(tmp_path, allow_cloud=False).allow_cloud is False
    assert MapRequest(tmp_path, allow_cloud=True).allow_cloud is True
    assert MapRequest(tmp_path, provider="ollama").model == "llama3.2"
    assert MapRequest(tmp_path, provider="gemini").model == "gemini-2.5-flash"
    with pytest.raises(ValueError, match="explicit routed model"):
        MapRequest(tmp_path, provider="openrouter")
    with pytest.raises(ValueError, match="boolean"):
        MapRequest.from_dict({"workspace": str(tmp_path), "allow_cloud": "maybe"})


def test_literature_policy_normalizes_equivalent_numeric_inputs_for_fingerprints() -> None:
    api_policy = LiteratureMappingPolicy.from_dict(
        {"literature_deadline_seconds": 1800, "deepseek_packet_context_fraction": 0.8}
    )
    cli_policy = LiteratureMappingPolicy.from_dict(
        {"literature_deadline_seconds": 1800.0, "deepseek_packet_context_fraction": 0.80}
    )

    assert api_policy == cli_policy
    assert api_policy.to_dict() == cli_policy.to_dict()
    assert api_policy.to_dict()["literature_deadline_seconds"] == 1800.0


def test_initialize_creates_compatible_file_first_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    manifest = initialize(workspace)
    assert manifest.status == "initialized"
    assert manifest.engine_version == "0.29.0"
    assert manifest.artifact_schema_version == "1.20"
    for relative in (
        "01_custody",
        "02_source_memory/notes",
        "02_source_memory/profiles",
        "03_literature_synthesis/maps",
        "11_state/runs",
        "11_state/legacy_maps",
    ):
        assert (workspace / relative).is_dir()
    config_text = (workspace / "auto-zettelkasten.yml").read_text()
    config = read_yaml(workspace / "auto-zettelkasten.yml")
    assert "API_KEY" not in config_text
    assert config["engine_version"] == "0.29.0"
    assert config["artifact_schema_version"] == "1.20"
    assert config["privacy"]["allow_cloud"] is False
    assert config["prompt_version"] == "11"
    assert config["extraction"]["version"] == "2"
    assert config["extraction"]["ocr"] == "auto"
    assert config["literature_mapping"]["synthesis_enabled"] is True
    assert config["literature_mapping"]["external_discovery"] == "disabled"


def test_newer_artifact_schema_is_rejected(tmp_path: Path) -> None:
    initialize(tmp_path)
    write_yaml(tmp_path / "11_state" / "workspace_manifest.yml", {"artifact_schema_version": "999.0"})
    config = read_yaml(tmp_path / "auto-zettelkasten.yml")
    config["artifact_schema_version"] = "999.0"
    write_yaml(tmp_path / "auto-zettelkasten.yml", config)
    with pytest.raises(IncompatibleArtifactSchemaError, match="newer"):
        assert_compatible(tmp_path)


def test_schema_1_0_workspace_remains_compatible(tmp_path: Path) -> None:
    initialize(tmp_path)
    write_yaml(tmp_path / "11_state" / "workspace_manifest.yml", {"artifact_schema_version": "1.0"})
    config = read_yaml(tmp_path / "auto-zettelkasten.yml")
    config["artifact_schema_version"] = "1.0"
    write_yaml(tmp_path / "auto-zettelkasten.yml", config)
    assert_compatible(tmp_path)


def test_schema_1_1_workspace_remains_compatible(tmp_path: Path) -> None:
    initialize(tmp_path)
    write_yaml(tmp_path / "11_state" / "workspace_manifest.yml", {"artifact_schema_version": "1.1"})
    config = read_yaml(tmp_path / "auto-zettelkasten.yml")
    config["artifact_schema_version"] = "1.1"
    write_yaml(tmp_path / "auto-zettelkasten.yml", config)
    assert_compatible(tmp_path)


def test_schema_1_2_workspace_remains_readable_until_local_migration(tmp_path: Path) -> None:
    initialize(tmp_path)
    manifest = read_yaml(tmp_path / "11_state" / "workspace_manifest.yml")
    config = read_yaml(tmp_path / "auto-zettelkasten.yml")
    manifest["artifact_schema_version"] = "1.2"
    config["artifact_schema_version"] = "1.2"
    write_yaml(tmp_path / "11_state" / "workspace_manifest.yml", manifest)
    write_yaml(tmp_path / "auto-zettelkasten.yml", config)
    assert_compatible(tmp_path)


@pytest.mark.parametrize(
    "schema_version",
    [
        "1.0",
        "1.1",
        "1.2",
        "1.3",
        "1.4",
        "1.5",
        "1.6",
        "1.7",
        "1.8",
        "1.9",
        "1.10",
        "1.11",
        "1.12",
        "1.13",
        "1.14",
        "1.15",
        "1.16",
        "1.17",
        "1.18",
        "1.20",
    ],
)
def test_all_supported_artifact_schemas_remain_readable(tmp_path: Path, schema_version: str) -> None:
    initialize(tmp_path)
    manifest_path = tmp_path / "11_state" / "workspace_manifest.yml"
    config_path = tmp_path / "auto-zettelkasten.yml"
    manifest = read_yaml(manifest_path)
    config = read_yaml(config_path)
    manifest["artifact_schema_version"] = schema_version
    config["artifact_schema_version"] = schema_version
    write_yaml(manifest_path, manifest)
    write_yaml(config_path, config)

    assert_compatible(tmp_path)


def test_schema_1_3_0_is_normalized_and_config_manifest_disagreement_fails(tmp_path: Path) -> None:
    initialize(tmp_path)
    manifest_path = tmp_path / "11_state" / "workspace_manifest.yml"
    config_path = tmp_path / "auto-zettelkasten.yml"
    manifest = read_yaml(manifest_path)
    config = read_yaml(config_path)
    manifest["artifact_schema_version"] = "1.3.0"
    config["artifact_schema_version"] = "1.3"
    write_yaml(manifest_path, manifest)
    write_yaml(config_path, config)
    assert_compatible(tmp_path)

    config["artifact_schema_version"] = "1.2"
    write_yaml(config_path, config)
    with pytest.raises(IncompatibleArtifactSchemaError, match="disagrees"):
        assert_compatible(tmp_path)


@pytest.mark.parametrize("malformed", ["garbage", "", "1", "1.x", "1.3.1"])
def test_malformed_schema_versions_fail_explicitly(tmp_path: Path, malformed: str) -> None:
    initialize(tmp_path)
    manifest_path = tmp_path / "11_state" / "workspace_manifest.yml"
    config_path = tmp_path / "auto-zettelkasten.yml"
    manifest = read_yaml(manifest_path)
    config = read_yaml(config_path)
    manifest["artifact_schema_version"] = malformed
    config["artifact_schema_version"] = malformed
    write_yaml(manifest_path, manifest)
    write_yaml(config_path, config)
    with pytest.raises(IncompatibleArtifactSchemaError, match="malformed"):
        assert_compatible(tmp_path)


def test_load_config_rejects_malformed_literature_policy(tmp_path: Path) -> None:
    initialize(tmp_path)
    config_path = tmp_path / "auto-zettelkasten.yml"
    config = read_yaml(config_path)
    config["literature_mapping"]["max_profile_calls"] = "100"
    write_yaml(config_path, config)
    with pytest.raises(ValueError, match="literature_mapping.max_profile_calls"):
        load_config(tmp_path)


def test_load_config_rejects_unknown_policy_fields(tmp_path: Path) -> None:
    initialize(tmp_path)
    config_path = tmp_path / "auto-zettelkasten.yml"
    config = read_yaml(config_path)
    config["literature_mapping"]["model_enthusiasm"] = 99
    write_yaml(config_path, config)
    with pytest.raises(ValueError, match="unknown literature_mapping fields"):
        load_config(tmp_path)
