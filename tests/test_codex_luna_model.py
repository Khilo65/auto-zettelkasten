from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_zettelkasten.cli import main
from auto_zettelkasten.models import (
    DEFAULT_CODEX_SOURCE_MODEL,
    LiteratureMappingPolicy,
    MapRequest,
)
from auto_zettelkasten.readers import (
    CODEX_CLI_PROFILES,
    CodexReader,
    ProviderError,
    codex_contract_identity,
    codex_preflight_status,
)
from auto_zettelkasten.workspace import initialize, load_config
from auto_zettelkasten.files import write_yaml


@pytest.mark.parametrize("cli_profile", ["0.145.0", "0.152.1"])
def test_luna_catalog_preflight_rejects_incompatible_capabilities(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cli_profile: str,
) -> None:
    executable = tmp_path / "codex"
    executable.touch()
    profile = CODEX_CLI_PROFILES[cli_profile]
    capability = {
        **profile["models"]["gpt-6-luna"],
        "supported_reasoning_levels": [{"effort": "medium"}],
    }
    features = "\n".join(
        f"{name}  {value['maturity']}  "
        f"{str(False if name in profile['tool_features'] else value['default']).lower()}"
        for name, value in profile["features"].items()
    )
    monkeypatch.setattr(
        "auto_zettelkasten.readers._codex_executable", lambda _env: executable
    )
    monkeypatch.setattr(
        "auto_zettelkasten.readers._codex_model_catalog",
        lambda _env: {"gpt-6-luna": capability},
    )

    def run(args: list[str], **_kwargs: object) -> SimpleNamespace:
        output = (
            f"codex-cli {cli_profile}" if args[-1] == "--version"
            else features if args[-2:] == ["features", "list"]
            else "Logged in using ChatGPT"
        )
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(
        subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("no inference")
    )
    status = codex_preflight_status("gpt-6-luna")
    assert status["context_window_compatibility"] is True
    assert status["reasoning_effort_compatibility"] is True
    for field, wrong in (("context_window", 1_050_000), ("tool_mode", "tools")):
        original = capability[field]
        capability[field] = wrong
        with pytest.raises(ProviderError, match="catalog is incompatible"):
            codex_preflight_status("gpt-6-luna")
        capability[field] = original
    capability["supported_reasoning_levels"] = [{"effort": "high"}]
    with pytest.raises(ProviderError, match="does not support medium"):
        codex_preflight_status("gpt-6-luna")


@pytest.mark.parametrize("command", ["map", "sync"])
@pytest.mark.parametrize("pinned", [None, "gpt-5.6-luna", "gpt-5.6-sol"])
def test_source_cli_defaults_to_current_luna_and_preserves_pins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    command: str, pinned: str | None, capsys: pytest.CaptureFixture[str],
) -> None:
    initialize(tmp_path)
    if pinned:
        config = load_config(tmp_path)
        config.update(provider="codex", model=pinned, literature_model="gpt-5.6-terra")
        write_yaml(tmp_path / "auto-zettelkasten.yml", config)
    captured = []

    def capture(request: MapRequest, **_kwargs: object) -> SimpleNamespace | dict[str, str]:
        captured.append(request)
        if command == "sync":
            return {"status": "completed"}
        return SimpleNamespace(to_dict=lambda: {"status": "completed"})

    monkeypatch.setattr(f"auto_zettelkasten.cli.{'run_map' if command == 'map' else 'sync_zotero'}", capture)
    assert main([
        command, "--workspace", str(tmp_path), "--provider", "codex",
        "--literature-model", "gpt-5.6-terra", "--allow-cloud",
    ]) == 0
    capsys.readouterr()
    assert captured[0].model == (pinned or "gpt-6-luna")
    assert captured[0].literature_model == "gpt-5.6-terra"


def test_new_luna_source_role_and_identity_separate_from_saved_model(tmp_path: Path) -> None:
    assert DEFAULT_CODEX_SOURCE_MODEL == "gpt-6-luna"
    request = MapRequest(
        tmp_path, provider="codex", model=DEFAULT_CODEX_SOURCE_MODEL,
        literature_model="gpt-5.6-terra",
    )
    assert MapRequest.from_dict(request.to_dict()).model == "gpt-6-luna"
    assert CodexReader(request.model).context_window_tokens == 272_000
    old = MapRequest.from_dict({**request.to_dict(), "model": "gpt-5.6-luna"})
    assert old.model == "gpt-5.6-luna"
    old_identity = codex_contract_identity("source_bundle", old.model, "medium")
    new_identity = codex_contract_identity("source_bundle", request.model, "medium")
    assert old_identity != new_identity
    assert {k: v for k, v in old_identity.items() if k != "model"} == {
        k: v for k, v in new_identity.items() if k != "model"
    }
    with pytest.raises(ValueError, match="literature_model must"):
        MapRequest(tmp_path, provider="codex", model="gpt-6-luna", literature_model="gpt-6-luna")
    MapRequest(
        tmp_path, provider="codex", model="gpt-6-luna",
        literature_policy=LiteratureMappingPolicy(synthesis_enabled=False),
    )
