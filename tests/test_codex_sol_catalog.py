from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_zettelkasten.readers import (
    CODEX_CLI_PROFILES,
    ProviderError,
    codex_preflight_status,
)


@pytest.mark.parametrize("maximum", [272_000, 872_000])
def test_sol_preflight_accepts_catalogued_normal_or_opt_in_maximum(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, maximum: int
) -> None:
    executable = tmp_path / "codex"
    executable.touch()
    profile = CODEX_CLI_PROFILES["0.145.0"]
    sol = profile["models"]["gpt-5.6-sol"]
    feature_output = "\n".join(
        f"{name}  {value['maturity']}  "
        f"{str(False if name in profile['tool_features'] else value['default']).lower()}"
        for name, value in profile["features"].items()
    )

    monkeypatch.setattr(
        "auto_zettelkasten.readers._codex_executable", lambda _environment: executable
    )

    def run(args: list[str], **_kwargs: object) -> SimpleNamespace:
        if args[-1] == "--version":
            output = "codex-cli 0.145.0"
        elif args[-2:] == ["features", "list"]:
            output = feature_output
        else:
            output = "Logged in using ChatGPT"
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(subprocess, "run", run)

    def catalog(maximum: int) -> dict[str, object]:
        return {
            "gpt-5.6-sol": {
                **sol,
                "max_context_window": maximum,
                "supported_reasoning_levels": [
                    {"effort": effort} for effort in sol["reasoning_efforts"]
                ],
            }
        }

    monkeypatch.setattr(
        "auto_zettelkasten.readers._codex_model_catalog",
        lambda _environment: catalog(maximum),
    )
    assert codex_preflight_status("gpt-5.6-sol", "max")[
        "context_window_compatibility"
    ]

    monkeypatch.setattr(
        "auto_zettelkasten.readers._codex_model_catalog",
        lambda _environment: catalog(1_000_000),
    )
    with pytest.raises(ProviderError, match="catalog is incompatible"):
        codex_preflight_status("gpt-5.6-sol", "max")
