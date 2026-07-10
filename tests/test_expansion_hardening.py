from __future__ import annotations

import urllib.error
from pathlib import Path

import pytest

from auto_zettelkasten.api import initialize_workspace, list_expansion_candidates, migrate_workspace
from auto_zettelkasten.citations import write_citation_sidecar
from auto_zettelkasten.files import read_yaml, write_yaml
from auto_zettelkasten.identity import identify_work
from auto_zettelkasten.models import ExpansionCandidate, ExpansionRequest
from auto_zettelkasten.scholarly import ScholarlyProviderError, SemanticScholarProvider
from auto_zettelkasten.workspace import IncompatibleArtifactSchemaError, assert_compatible, require_schema


class _Response:
    status = 200

    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self.payload


def test_persisted_network_consent_is_never_replayed_and_targets_are_canonical(tmp_path: Path) -> None:
    payload = {
        "workspace": str(tmp_path),
        "scope": "source",
        "target_ids": ["source-b", "source-a", "source-b"],
        "provider": "semantic-scholar",
        "allow_network": True,
    }
    with pytest.raises(ValueError, match="allow_network"):
        ExpansionRequest.from_dict(payload)
    request = ExpansionRequest.from_dict(payload, allow_network=True)
    assert request.target_ids == ("source-a", "source-b")
    with pytest.raises(ValueError, match="opaque"):
        ExpansionRequest(tmp_path, scope="source", target_ids=("../escape",))


def test_candidate_validation_and_decision_ledger_controls_state(tmp_path: Path) -> None:
    initialize_workspace(tmp_path)
    valid = {
        "work_id": "work-doi-abc",
        "suggestion_id": "suggestion-abc",
        "target_scope": "source",
        "target_id": "source-a",
        "target_ids": ["source-a"],
        "primary_relation": "cites",
        "score": 0.5,
        "actionability": "ready",
        "state": "accepted",
    }
    ExpansionCandidate.from_dict(valid)
    with pytest.raises(ValueError, match="exactly one"):
        ExpansionCandidate.from_dict({**valid, "target_ids": ["source-a", "source-b"]})
    with pytest.raises(ValueError, match="state"):
        ExpansionCandidate.from_dict({**valid, "state": "qualified"})

    candidates_path = tmp_path / "03_literature_synthesis" / "expansion" / "candidates.yml"
    write_yaml(candidates_path, {"artifact_schema_version": "1.1", "candidates": [valid]})
    # Materialized state cannot manufacture acceptance without an append-only decision.
    [loaded] = list_expansion_candidates(tmp_path)
    assert loaded.state == "proposed"
    assert loaded.decision_version == 0


def test_schema_gates_reject_missing_malformed_and_future_versions(tmp_path: Path) -> None:
    initialize_workspace(tmp_path)
    manifest_path = tmp_path / "11_state" / "workspace_manifest.yml"
    manifest = read_yaml(manifest_path)
    for version in ("", "one.one", "1.2"):
        write_yaml(manifest_path, {**manifest, "artifact_schema_version": version})
        with pytest.raises(IncompatibleArtifactSchemaError):
            assert_compatible(tmp_path)
        with pytest.raises(IncompatibleArtifactSchemaError):
            require_schema(tmp_path, "1.1", operation="test")
        with pytest.raises(IncompatibleArtifactSchemaError):
            migrate_workspace(tmp_path, target="1.1")


def test_title_tuple_stays_unresolved_until_reconciled_and_sidecars_are_confined(tmp_path: Path) -> None:
    _, actionability = identify_work({"title": "Same title", "year": "2024", "authors": ["A Author"]})
    assert actionability == "resolve_identity"
    initialize_workspace(tmp_path)
    with pytest.raises(ValueError, match="source_id"):
        write_citation_sidecar(
            tmp_path,
            item={"key": "ITEM", "data": {"key": "ITEM"}},
            source_id="../outside",
            text="References\nAuthor. (2024). Work.",
            content_hash="a" * 64,
            source_file="zotero://select/library/items/ITEM",
        )


def test_work_identity_precedence_is_stable() -> None:
    base = {
        "title": "Identity",
        "year": "2024",
        "authors": ["A Author"],
        "isbn": "978-1-4028-9462-6",
        "url": "https://example.org/work",
        "provider_ids": {"semantic_scholar": "S2-WORK"},
        "doi": "10.5555/identity",
    }
    assert identify_work(base)[0].startswith("work-doi-")
    assert identify_work({key: value for key, value in base.items() if key != "doi"})[0].startswith("work-s2-")
    assert identify_work({key: value for key, value in base.items() if key not in {"doi", "provider_ids"}})[0].startswith("work-url-")
    assert identify_work({key: value for key, value in base.items() if key not in {"doi", "provider_ids", "url"}})[0].startswith("work-isbn-")
    assert identify_work({key: value for key, value in base.items() if key in {"title", "year", "authors"}})[0].startswith("work-title-")


def test_provider_records_invalid_json_as_failure_and_propagates_transport_errors() -> None:
    provider = SemanticScholarProvider(opener=lambda *_args, **_kwargs: _Response(b"not-json"), max_retries=1)
    with pytest.raises(ScholarlyProviderError, match="invalid JSON"):
        provider.resolve_work({"doi": "10.5555/test"})
    assert provider.attempts[-1]["status"] == "failed"
    assert provider.attempts[-1]["reason"] == "invalid_json"

    def unavailable(*_args, **_kwargs):
        raise urllib.error.URLError("offline")

    provider = SemanticScholarProvider(opener=unavailable, max_retries=1)
    with pytest.raises(ScholarlyProviderError, match="cannot reach"):
        provider.resolve_work({"doi": "10.5555/test"})
