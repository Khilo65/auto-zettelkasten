from __future__ import annotations

from pathlib import Path

import pytest

from auto_zettelkasten import ARTIFACT_SCHEMA_VERSION, ENGINE_VERSION
from auto_zettelkasten.api import initialize_workspace
from auto_zettelkasten.models import ExpansionRequest, MapRequest
from auto_zettelkasten.workspace import IncompatibleArtifactSchemaError, assert_compatible
from auto_zettelkasten.files import write_yaml


def test_map_request_is_versioned_serializable_and_validated(tmp_path: Path) -> None:
    request = MapRequest(tmp_path, scope="collection", collection_key="C1", limit=3)
    assert MapRequest.from_dict(request.to_dict()) == request
    with pytest.raises(ValueError, match="collection_key"):
        MapRequest(tmp_path, scope="collection")
    with pytest.raises(ValueError, match="limit"):
        MapRequest(tmp_path, limit=-1)
    assert MapRequest.from_dict({"workspace": str(tmp_path), "allow_cloud": "false"}).allow_cloud is False
    assert MapRequest(tmp_path, provider="ollama").model == "llama3.2"
    assert MapRequest(tmp_path, provider="gemini").model == "gemini-2.5-flash"
    with pytest.raises(ValueError, match="explicit routed model"):
        MapRequest(tmp_path, provider="openrouter")
    with pytest.raises(ValueError, match="boolean"):
        MapRequest.from_dict({"workspace": str(tmp_path), "allow_cloud": "maybe"})


def test_direct_requests_require_actual_boolean_consent(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="allow_cloud must be a boolean"):
        MapRequest(tmp_path, allow_cloud="false")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="allow_network must be a boolean"):
        ExpansionRequest(
            tmp_path,
            scope="source",
            target_ids=("source-seed",),
            provider="semantic-scholar",
            allow_network="false",  # type: ignore[arg-type]
        )


def test_initialize_creates_compatible_file_first_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    manifest = initialize_workspace(workspace)
    assert manifest.status == "initialized"
    assert manifest.engine_version == ENGINE_VERSION
    assert manifest.artifact_schema_version == ARTIFACT_SCHEMA_VERSION
    for relative in ("01_custody", "02_source_memory/notes", "03_literature_synthesis", "11_state/runs"):
        assert (workspace / relative).is_dir()
    config = (workspace / "auto-zettelkasten.yml").read_text()
    assert "API_KEY" not in config
    assert "allow_cloud: false" in config


def test_newer_artifact_schema_is_rejected(tmp_path: Path) -> None:
    initialize_workspace(tmp_path)
    write_yaml(tmp_path / "11_state" / "workspace_manifest.yml", {"artifact_schema_version": "999.0"})
    with pytest.raises(IncompatibleArtifactSchemaError, match="newer"):
        assert_compatible(tmp_path)
