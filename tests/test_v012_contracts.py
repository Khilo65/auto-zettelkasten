from __future__ import annotations

from auto_zettelkasten import ARTIFACT_SCHEMA_VERSION, ENGINE_VERSION
from auto_zettelkasten.cli import build_parser
from auto_zettelkasten.models import MapRequest


def test_v013_versions() -> None:
    assert ENGINE_VERSION == "0.29.1"
    assert ARTIFACT_SCHEMA_VERSION == "1.20"


def test_map_request_round_trips_terminal_retry_flag(tmp_path) -> None:
    request = MapRequest(
        workspace=tmp_path,
        retry_terminal_failures=True,
    )

    restored = MapRequest.from_dict(request.to_dict())

    assert restored.retry_terminal_failures is True


def test_retry_terminal_literature_cli_flags() -> None:
    parser = build_parser()

    build_args = parser.parse_args(
        [
            "build-map",
            "--workspace",
            "/tmp/workspace",
            "--retry-terminal-literature",
        ]
    )
    resume_args = parser.parse_args(
        [
            "resume",
            "--workspace",
            "/tmp/workspace",
            "--run-id",
            "run-1",
            "--retry-terminal-literature",
        ]
    )

    assert build_args.retry_terminal_literature is True
    assert resume_args.retry_terminal_literature is True
