from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

import auto_zettelkasten.readers as readers
from auto_zettelkasten.readers import (
    CODEX_CLI_PROFILES,
    CodexReader,
    ProviderIsolationFailure,
    ProviderTransportError,
    _codex_payload_sentinels,
    _codex_pdf_helper_manifest,
    _codex_tool_feature_arguments,
    _redact_codex_diagnostic,
    _validated_codex_pdf_attachment,
)


def test_codex_0152_feature_snapshot_and_tool_disables_are_profile_specific() -> None:
    feature_hash = hashlib.sha256(
        json.dumps(
            CODEX_CLI_PROFILES["0.152.1"]["features"],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert (
        feature_hash
        == "8ac2a0c4033a30657208b09ade9ca898d5f9bbe4569f21ae244daf6b4bfd2aff"
    )
    assert "features.view_image=false" in _codex_tool_feature_arguments("0.152.1")
    assert "features.sleep_tool=false" in _codex_tool_feature_arguments("0.152.1")
    assert "features.unbounded_connection_retries=false" in (
        _codex_tool_feature_arguments("0.152.1")
    )
    assert "features.unified_exec=false" not in _codex_tool_feature_arguments("0.152.1")
    assert "features.unified_exec=false" in _codex_tool_feature_arguments("0.145.0")
    assert "features.view_image=false" not in _codex_tool_feature_arguments("0.145.0")
    assert "features.sleep_tool=false" not in _codex_tool_feature_arguments("0.145.0")


def test_pdf_helper_manifest_requires_package_trust(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executable = tmp_path / "auto-zettelkasten-codex"
    executable.write_bytes(b"reviewed helper binary")
    executable.chmod(0o755)
    patch_hash = "1" * 64
    binary_hash = hashlib.sha256(executable.read_bytes()).hexdigest()
    manifest = {
        "manifest_version": 1,
        "upstream_tag": "rust-v0.152.1",
        "upstream_commit": "5adb68a49933ae446bf11935662c83dba55a0804",
        "platform": "macos-arm64",
        "license": "Apache-2.0",
        "notice": "NOTICE",
        "input_file_protocol_revision": "input_file-v1",
        "patch_sha256": patch_hash,
        "binary_sha256": binary_hash,
    }
    executable.with_name(executable.name + ".manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    monkeypatch.setattr(readers.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(readers.platform, "machine", lambda: "arm64")

    assert _codex_pdf_helper_manifest(executable) is None
    monkeypatch.setattr(
        readers,
        "_CODEX_PDF_HELPER_TRUST",
        {"macos-arm64": frozenset({(patch_hash, binary_hash)})},
    )
    identity = _codex_pdf_helper_manifest(executable)
    assert identity is not None
    assert identity["patch_sha256"] == patch_hash
    assert identity["binary_sha256"] == binary_hash


def test_pdf_leak_sentinels_cover_start_middle_and_end_and_diagnostics_redact() -> None:
    payload = bytes(range(256)) * 2
    encoded = base64.b64encode(payload)
    sentinels = _codex_payload_sentinels(encoded)
    assert sentinels == (
        encoded[:64],
        encoded[(len(encoded) - 64) // 2 : (len(encoded) - 64) // 2 + 64],
        encoded[-64:],
    )
    diagnostic = (
        "data:application/pdf;base64," + encoded.decode() + "\n" + encoded.decode()
    )
    redacted = _redact_codex_diagnostic(diagnostic)
    assert encoded.decode() not in redacted
    assert "data:application/pdf;base64,[REDACTED]" in redacted


def test_pdf_attachment_must_still_match_its_custody_hash(tmp_path: Path) -> None:
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-1.7\n%%EOF\n")

    with pytest.raises(ProviderIsolationFailure, match="custody hash mismatch"):
        _validated_codex_pdf_attachment((pdf,), None, "0" * 64)


def test_pdf_spawn_failure_is_typed_after_attempt_reservation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executable = tmp_path / "auto-zettelkasten-codex"
    executable.write_bytes(b"helper")
    executable.chmod(0o755)
    credential_root = tmp_path / "credential-root"
    credential_root.mkdir()
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-1.7\n" + bytes(range(128)) + b"\n%%EOF\n")
    helper_identity = {
        "manifest_version": 1,
        "upstream_tag": "rust-v0.152.1",
        "upstream_commit": "5adb68a49933ae446bf11935662c83dba55a0804",
        "platform": "macos-arm64",
        "license": "Apache-2.0",
        "notice": "NOTICE",
        "input_file_protocol_revision": "input_file-v1",
        "patch_sha256": "1" * 64,
        "binary_sha256": "2" * 64,
        "manifest_sha256": "3" * 64,
    }
    reader = CodexReader("gpt-5.6-luna", allow_cloud=True)
    reader._preflight = {
        "executable": str(executable),
        "version": "0.152.1",
        "pdf_input_file_capability": True,
        "_helper_manifest_identity": helper_identity,
        "_credential_root": str(credential_root),
        "_environment": {"PATH": ""},
    }
    monkeypatch.setattr(
        readers, "_codex_pdf_helper_manifest", lambda _path: helper_identity
    )

    def isolate(
        _environment: object,
        _credential_root: object,
        call_root: Path,
        _deadline: float,
    ) -> dict[str, str]:
        child_home = call_root / "home"
        child_codex_home = child_home / ".codex"
        child_codex_home.mkdir(parents=True)
        (child_codex_home / "auth.json").write_bytes(b"unchanged")
        return {"HOME": str(child_home), "CODEX_HOME": str(child_codex_home)}

    events: list[str] = []
    monkeypatch.setattr(readers, "_isolated_codex_environment", isolate)
    monkeypatch.setattr(
        readers,
        "reserve_codex_attempt",
        lambda *_args, **_kwargs: events.append("reserved"),
    )

    def fail_spawn(*_args: object, **_kwargs: object) -> subprocess.Popen[bytes]:
        events.append("spawned")
        raise FileNotFoundError("gone")

    monkeypatch.setattr(subprocess, "Popen", fail_spawn)
    token = readers._OUTPUT_CONTRACT.set("source_bundle")
    try:
        with pytest.raises(ProviderTransportError) as raised:
            reader._generate_pdf_text("system", "user", 128, 5, (pdf,))
    finally:
        readers._OUTPUT_CONTRACT.reset(token)
    assert raised.value.transport_kind == "codex_app_server"
    assert raised.value.cause_type == "FileNotFoundError"
    assert events == ["reserved", "spawned"]


def test_pdf_spawn_rejects_cached_self_attested_helper_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executable = tmp_path / "auto-zettelkasten-codex"
    executable.write_bytes(b"untrusted helper")
    executable.chmod(0o755)
    credential_root = tmp_path / "credential-root"
    credential_root.mkdir()
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-1.7\n%%EOF\n")
    reader = CodexReader("gpt-5.6-luna", allow_cloud=True)
    reader._preflight = {
        "executable": str(executable),
        "version": "0.152.1",
        "pdf_input_file_capability": True,
        "_helper_manifest_identity": {"manifest_sha256": "1" * 64},
        "_credential_root": str(credential_root),
        "_environment": {"PATH": ""},
    }
    monkeypatch.setattr(readers, "_codex_pdf_helper_manifest", lambda _path: None)
    monkeypatch.setattr(
        readers,
        "reserve_codex_attempt",
        lambda *_args, **_kwargs: pytest.fail("untrusted helper reserved an attempt"),
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("untrusted helper was started"),
    )
    token = readers._OUTPUT_CONTRACT.set("source_bundle")
    try:
        with pytest.raises(ProviderIsolationFailure, match="identity changed"):
            reader._generate_pdf_text("system", "user", 128, 5, (pdf,))
    finally:
        readers._OUTPUT_CONTRACT.reset(token)
