from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "v029_prepare_frozen_sample",
    Path(__file__).parents[1] / "tools/v029_prepare_frozen_sample.py",
)
assert SPEC and SPEC.loader
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)


def test_historical_benchmark_cli_path_is_optional(tmp_path, monkeypatch) -> None:
    calls: list[tuple[Path, Path, Path | None]] = []

    def fake_prepare(
        origin: Path,
        target: Path,
        *,
        historical_benchmark: Path | None = None,
    ) -> None:
        calls.append((origin, target, historical_benchmark))

    monkeypatch.setattr(tool, "prepare", fake_prepare)
    origin = tmp_path / "origin"
    target = tmp_path / "target"
    monkeypatch.setattr(
        sys,
        "argv",
        ["v029_prepare_frozen_sample.py", "--origin", str(origin), "--target", str(target)],
    )
    tool.main()
    assert calls == [(origin.resolve(), target.resolve(), None)]

    benchmark = tmp_path / "benchmark.yml"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "v029_prepare_frozen_sample.py",
            "--origin",
            str(origin),
            "--target",
            str(target),
            "--historical-benchmark",
            str(benchmark),
        ],
    )
    tool.main()
    assert calls[-1] == (origin.resolve(), target.resolve(), benchmark.resolve())
