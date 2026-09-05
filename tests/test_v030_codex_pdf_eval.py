from __future__ import annotations

import importlib.util
import json
import marshal
import py_compile
import sys
import zipfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from auto_zettelkasten.api import initialize_workspace
from auto_zettelkasten.codex_attempt_guard import (
    CodexAttemptDeny,
    CodexAttemptStateError,
    reserve_codex_attempt,
)
from auto_zettelkasten.files import append_jsonl, read_yaml, sha256_file, write_yaml
from auto_zettelkasten.workspace import IncompatibleArtifactSchemaError


TOOLS = Path(__file__).parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
SPEC = importlib.util.spec_from_file_location(
    "v030_codex_pdf_eval",
    TOOLS / "v030_codex_pdf_eval.py",
)
assert SPEC and SPEC.loader
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)
CODE_COMMIT = "a" * 40


def _clean_repo() -> tuple[str, bool]:
    return CODE_COMMIT, False


class _FakeAttemptGuard:
    def __init__(self, events: list[tuple[Any, ...]]) -> None:
        self.events = events

    @contextmanager
    def activate(self) -> Any:
        self.events.append(("activate",))
        yield self

    def finish(self, state: str, *, reason: str = "") -> None:
        self.events.append(("finish", state, reason))


def _guard_factory(
    events: list[tuple[Any, ...]],
) -> Any:
    def factory(path: Path, digest: str, **kwargs: Any) -> _FakeAttemptGuard:
        assert kwargs["evaluation_id"] == "synthetic-four-pdf"
        assert kwargs["run_id"] == "synthetic-four-pdf-run"
        assert kwargs["source_attempt_limit"] == 6
        assert kwargs["relationship_attempt_limit"] == 8
        assert kwargs["total_attempt_limit"] == 14
        events.append(
            (
                "start",
                path,
                digest,
                kwargs["stage"],
                kwargs["resume_reason"],
            )
        )
        return _FakeAttemptGuard(events)

    return factory


def _authorization(root: Path) -> tuple[Path, str]:
    path = root / "PRIVATE_AUTHORIZATION.json"
    path.write_text("{}\n", encoding="utf-8")
    return path, sha256_file(path)


def _manifest(root: Path) -> Path:
    custody = root / "01_custody" / "files"
    custody.mkdir(parents=True)
    initialize_workspace(root)
    cases = []
    for index in range(4):
        parent_key = f"P{index + 1}"
        attachment_key = f"A{index + 1}"
        path = custody / f"case-{index + 1}.pdf"
        path.write_bytes(f"synthetic PDF {index + 1}".encode())
        route = runner.FOUR_PDF_ROUTE_ORACLE[index]
        cases.append(
            {
                "case_id": f"case-{index + 1}",
                "pdf": str(path.relative_to(root)),
                "sha256": sha256_file(path),
                "expected": {
                    "content_route": route,
                    "selected_pages": [],
                    "audited_facts": [f"Synthetic source {index + 1}"],
                    "audited_locators": [f"Audited locator {index + 1}"],
                    "expected_answers": [f"Expected answer {index + 1}"],
                },
                "zotero_parent": {
                    "key": parent_key,
                    "data": {
                        "key": parent_key,
                        "itemType": "journalArticle",
                        "title": f"Synthetic source {index + 1}",
                        "date": "2026",
                        "creators": [],
                    },
                },
                "zotero_attachment": {
                    "key": attachment_key,
                    "data": {
                        "key": attachment_key,
                        "parentItem": parent_key,
                        "itemType": "attachment",
                        "contentType": "application/pdf",
                        "filename": path.name,
                    },
                },
            }
        )
    path = root / "PRIVATE_MANIFEST.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "code_commit": CODE_COMMIT,
                "evaluation_id": "synthetic-four-pdf",
                "run_id": "synthetic-four-pdf-run",
                "workspace": str(root),
                "question": "How do the four synthetic sources relate?",
                "cases": cases,
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _helper_identity() -> dict[str, Any]:
    return {
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


def _completion(
    *,
    source: bool,
    contract_id: str,
    cli_version: str = runner.DIRECT_PDF_CLI_VERSION,
    pdf_hash: str = "",
) -> dict[str, Any]:
    model = runner.SOURCE_MODEL if source else runner.RELATIONSHIP_MODEL
    identity = runner.codex_contract_identity(
        contract_id, model, runner.REASONING_EFFORT, cli_version
    )
    completion = {
        **identity,
        "codex_cli_version": cli_version,
        "finish_reason": "turn.completed",
        "max_output_tokens": identity["output_reservation"],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    if pdf_hash:
        helper = _helper_identity()
        completion.update(
            attachment_transport={
                **runner.codex_source_bundle_attachment_identity(
                    runner.DIRECT_PDF_CLI_VERSION, helper
                ),
                "adapter_protocol": "codex-app-server-jsonrpc-v2",
            },
            attachment_count=1,
            attachment_hashes=[pdf_hash],
        )
    return completion


def _usage_row(
    number: int,
    *,
    source: bool,
    contract_id: str,
    cli_version: str = runner.DIRECT_PDF_CLI_VERSION,
    pdf_hash: str = "",
) -> dict[str, Any]:
    return {
        "attempt_id": f"attempt-{source}-{number}",
        "stage": contract_id,
        "key": f"key-{number}",
        "fingerprint": f"fingerprint-{number}",
        "attempt": 1,
        "status": "completed",
        "provider_completion": _completion(
            source=source,
            contract_id=contract_id,
            cli_version=cli_version,
            pdf_hash=pdf_hash,
        ),
    }


def test_four_pdf_source_contract_remains_source_bundle_only() -> None:
    hierarchical = _usage_row(1, source=True, contract_id="chunk_evidence")

    assert runner._completion_error(hierarchical, source=True) == (
        "source_contract_mismatch"
    )
    assert (
        runner._completion_error(
            hierarchical,
            source=True,
            settings=runner.GateSettings(kind="raw_e2e"),
        )
        == ""
    )


def test_four_pdf_manifest_requires_the_direct_pdf_route_oracle(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path / "private")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["cases"][0]["expected"]["content_route"] = runner.PDF_INPUT_ROUTE
    payload["cases"][1]["expected"]["content_route"] = runner.TEXT_ROUTE
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="four-PDF routes must be"):
        runner._validated_manifest(manifest, sha256_file(manifest))


def test_controlled_pdf_gate_is_one_direct_pdf_attempt(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path / "private")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["cases"] = [payload["cases"][1]]
    payload["cases"][0]["expected"]["content_route"] = runner.PDF_INPUT_ROUTE
    payload["gate"] = runner.CONTROLLED_PDF_GATE.manifest_binding()
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    validated, cases, _workspace = runner._validated_manifest(
        manifest,
        sha256_file(manifest),
        runner.CONTROLLED_PDF_GATE,
    )

    assert validated["gate"] == {
        "schema_version": "1",
        "kind": "controlled_pdf",
        "stage": "controlled_real_pdf_smoke",
        "case_count": 1,
        "source_attempt_limit": 1,
        "relationship_attempt_limit": 0,
        "total_attempt_limit": 1,
        "document_attempt_limit": 1,
        "stage_deadline_seconds": 8_460,
        "cluster_generation_enabled": False,
    }
    assert cases[0]["expected_route"] == runner.PDF_INPUT_ROUTE

    payload["cases"][0]["expected"]["content_route"] = runner.TEXT_ROUTE
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(
        ValueError, match="controlled PDF gate requires codex_pdf_input_file"
    ):
        runner._validated_manifest(
            manifest,
            sha256_file(manifest),
            runner.CONTROLLED_PDF_GATE,
        )

    payload["cases"][0]["expected"]["content_route"] = runner.PDF_INPUT_ROUTE
    payload["question"] = ""
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest question is required"):
        runner._validated_manifest(
            manifest,
            sha256_file(manifest),
            runner.CONTROLLED_PDF_GATE,
        )


def _installed_runtime(
    root: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, dict[str, Any], Path]:
    manifest = _manifest(root / "workspace")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["cases"] = [payload["cases"][1]]
    payload["gate"] = runner.CONTROLLED_PDF_GATE.manifest_binding()
    site = root / "venv" / "lib" / "site-packages"
    dist = "auto_zettelkasten-0.30.0.dist-info"
    files = {
        "auto_zettelkasten/__init__.py": b'ENGINE_VERSION = "0.30.0"\n',
        f"{dist}/METADATA": b"Name: auto-zettelkasten\nVersion: 0.30.0\n",
        f"{dist}/WHEEL": b"Wheel-Version: 1.0\n",
        f"{dist}/entry_points.txt": b"[console_scripts]\n",
        f"{dist}/licenses/LICENSE": b"Synthetic license\n",
        f"{dist}/RECORD": b"",
    }
    wheel = root / "auto_zettelkasten-0.30.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
            path = site / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    (site / dist / "INSTALLER").write_bytes(b"pip\n")
    (site / dist / "REQUESTED").write_bytes(b"")
    (site / dist / "RECORD").write_bytes(b"installer-specific record\n")
    wheel_binding = {"path": str(wheel), "sha256": sha256_file(wheel)}
    audit = root / "PACKAGE_AUDIT.yml"
    write_yaml(audit, {
        "package_audit_schema_version": "1", "status": "passed",
        "repository_dirty": False, "repository_head": CODE_COMMIT,
        "provider_calls": 0,
        "distribution": "auto-zettelkasten", "version": "0.30.0",
        "findings": [], "wheel": wheel_binding,
    })
    payload["installed_runtime"] = {
        "schema_version": "1", "wheel": wheel_binding,
        "package_audit": {"path": str(audit), "sha256": sha256_file(audit)},
    }
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    module = SimpleNamespace(
        __file__=str(site / "auto_zettelkasten" / "__init__.py"),
        __version__="0.30.0", ENGINE_VERSION="0.30.0",
    )
    module.__spec__ = SimpleNamespace(origin=module.__file__)
    monkeypatch.setattr(runner, "auto_zettelkasten", module)
    monkeypatch.setattr(runner, "sys", SimpleNamespace(
        modules={"auto_zettelkasten": module},
    ))
    monkeypatch.setattr(runner.sysconfig, "get_path", lambda key: str(site))
    return manifest, payload, site


def test_installed_runtime_verifies_wheel_and_preserves_default_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _manifest_path, payload, site = _installed_runtime(tmp_path, monkeypatch)
    source = site / "auto_zettelkasten" / "__init__.py"
    py_compile.compile(str(source), doraise=True)
    direct_url_path = site / "auto_zettelkasten-0.30.0.dist-info" / "direct_url.json"
    direct_url_path.write_text(json.dumps({
        "url": Path(payload["installed_runtime"]["wheel"]["path"]).as_uri(),
        "archive_info": {"hashes": {"sha256": payload["installed_runtime"]["wheel"]["sha256"]}},
    }))

    identity = runner._verify_runtime_import_root(payload, runner.CONTROLLED_PDF_GATE)
    assert identity == {
        "schema_version": "1", "version": "0.30.0",
        "wheel_sha256": payload["installed_runtime"]["wheel"]["sha256"],
        "package_audit_sha256": payload["installed_runtime"]["package_audit"]["sha256"],
        "package_file_count": 1,
    }
    with pytest.raises(RuntimeError, match="repository's src directory"):
        runner._verify_runtime_import_root()


@pytest.mark.parametrize("defect", [
    "wheel_hash", "audit_hash", "audit_failed", "audit_dirty", "audit_commit",
    "audit_version", "audit_wheel", "runtime_version", "altered", "missing",
    "extra", "symlink", "directory_symlink", "wheel_symlink", "orphan_bytecode",
    "tampered_bytecode", "metadata", "installer_extra", "wrong_origin", "wrong_site", "wrong_gate",
    "binding_schema", "unreadable_audit", "direct_url", "direct_url_shape",
])
def test_installed_runtime_rejects_defects_before_attempt_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str,
) -> None:
    manifest, payload, site = _installed_runtime(tmp_path, monkeypatch)
    binding = payload["installed_runtime"]
    source = site / "auto_zettelkasten" / "__init__.py"
    dist = site / "auto_zettelkasten-0.30.0.dist-info"
    settings = runner.CONTROLLED_PDF_GATE
    if defect in {"wheel_hash", "audit_hash"}:
        binding["wheel" if defect == "wheel_hash" else "package_audit"]["sha256"] = "0" * 64
    elif defect.startswith("audit_"):
        path = Path(binding["package_audit"]["path"])
        audit = read_yaml(path)
        key, value = {
            "audit_failed": ("status", "failed"),
            "audit_dirty": ("repository_dirty", True),
            "audit_commit": ("repository_head", "b" * 40),
            "audit_version": ("version", "0.29.11"),
            "audit_wheel": ("wheel", {"path": str(tmp_path / "other.whl"), "sha256": "0" * 64}),
        }[defect]
        audit[key] = value
        write_yaml(path, audit)
        binding["package_audit"]["sha256"] = sha256_file(path)
    elif defect == "runtime_version":
        runner.auto_zettelkasten.ENGINE_VERSION = "0.29.11"
    elif defect == "binding_schema":
        binding["unrecognized"] = True
    elif defect == "unreadable_audit":
        path = Path(binding["package_audit"]["path"])
        path.write_text("private_fixture_material: [\n")
        binding["package_audit"]["sha256"] = sha256_file(path)
    elif defect in {"direct_url", "direct_url_shape"}:
        (dist / "direct_url.json").write_text(json.dumps({
            "url": Path(binding["wheel"]["path"]).as_uri(),
            "archive_info": {"hashes": {"sha256": "0" * 64}} if defect == "direct_url" else ["hash"],
        }))
    elif defect == "altered":
        source.write_bytes(b"modified runtime")
    elif defect == "missing":
        source.unlink()
    elif defect == "extra":
        source.with_name("unexpected.py").write_bytes(b"extra runtime")
    elif defect == "symlink":
        target = tmp_path / "copied.py"
        target.write_bytes(source.read_bytes())
        source.unlink()
        source.symlink_to(target)
    elif defect == "directory_symlink":
        target = tmp_path / "copied-package"
        source.parent.rename(target)
        source.parent.symlink_to(target, target_is_directory=True)
    elif defect == "wheel_symlink":
        path = Path(binding["wheel"]["path"])
        target = path.with_name("copied.whl")
        path.rename(target)
        path.symlink_to(target)
    elif defect == "orphan_bytecode":
        path = source.parent / "__pycache__" / "orphan.cpython-313.pyc"
        path.parent.mkdir()
        path.write_bytes(b"orphan bytecode")
    elif defect == "tampered_bytecode":
        cache = Path(importlib.util.cache_from_source(str(source)))
        cache.parent.mkdir()
        cache.write_bytes(importlib.util.MAGIC_NUMBER + bytes(12) + marshal.dumps(
            compile("unexpected = True\n", str(source), "exec"),
        ))
    elif defect == "metadata":
        (dist / "METADATA").write_bytes(b"Version: 0.29.11\n")
    elif defect == "installer_extra":
        (dist / "unexpected.pth").write_bytes(b"unexpected")
    elif defect == "wrong_origin":
        runner.sys.modules["auto_zettelkasten.shadow"] = SimpleNamespace(
            __file__=str(tmp_path / "shadow.py"),
            __spec__=SimpleNamespace(origin=str(tmp_path / "shadow.py")),
        )
    elif defect == "wrong_site":
        monkeypatch.setattr(runner.sysconfig, "get_path", lambda key: str(tmp_path / "elsewhere"))
    elif defect == "wrong_gate":
        settings = runner.GateSettings(**{
            name: "another_controlled_gate" if name == "stage" else getattr(settings, name)
            for name in settings.__slots__
        })
        payload["gate"] = settings.manifest_binding()
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    events: list[str] = []

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        events.append("called")
        raise AssertionError("runtime must be checked before reservation or map")

    with pytest.raises((ValueError, RuntimeError), match="installed runtime"):
        runner.run_gate(
            mode="run", manifest_path=manifest, manifest_sha256=sha256_file(manifest),
            authorization_path=tmp_path / "unused-auth.json", authorization_sha256="1" * 64,
            execute=True, settings=settings, repository_probe=_clean_repo,
            attempt_guard_factory=forbidden, map_runner=forbidden,
        )
    assert events == []
    assert not (manifest.parent / settings.attempt_ledger_name).exists()


def test_installed_controlled_pdf_run_and_replay_recheck_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, payload, site = _installed_runtime(tmp_path, monkeypatch)
    authorization, authorization_sha256 = _authorization(tmp_path)
    settings = runner.CONTROLLED_PDF_GATE
    workspace = manifest.parent
    run_id = payload["run_id"]
    run_root = workspace / "11_state" / "runs" / run_id
    events: list[tuple[Any, ...]] = []
    acceptance = {"source_attempt_count": 1, "relationship_attempt_count": 0, "total_attempt_count": 1}
    monkeypatch.setattr(runner, "_acceptance", lambda *args: ([], acceptance))
    monkeypatch.setattr(runner, "_attempts", lambda *args: (
        {"count": int((run_root / "inventory.json").is_file()), "rows": []},
        {"count": 0, "rows": []},
    ))

    def factory(*_args: Any, **kwargs: Any) -> _FakeAttemptGuard:
        assert kwargs["total_attempt_limit"] == 1
        assert kwargs["source_attempt_limit"] == 1
        assert kwargs["relationship_attempt_limit"] == 0
        events.append(("start",))
        return _FakeAttemptGuard(events)

    def fake_map(request: Any, *, resume: bool, reader: Any, **_kwargs: Any) -> Any:
        assert Path(request.workspace) == workspace
        events.append(("map", resume))
        if resume:
            assert isinstance(reader, runner._ReplayCodexReader)
            assert isinstance(reader.attempt_guard, CodexAttemptDeny)
            return read_yaml(run_root / "run_report.yml")
        assert isinstance(reader, runner._ControlledPdfReader)
        run_root.mkdir(parents=True)
        (run_root / "inventory.json").write_text("[]\n")
        report = {"status": "completed", **acceptance}
        write_yaml(run_root / "run_report.yml", report)
        return report

    kwargs = dict(
        manifest_path=manifest, manifest_sha256=sha256_file(manifest),
        execute=True, settings=settings, repository_probe=_clean_repo, map_runner=fake_map,
    )
    _, first = runner.run_gate(
        mode="run", authorization_path=authorization,
        authorization_sha256=authorization_sha256, attempt_guard_factory=factory, **kwargs,
    )
    assert first["status"] == "passed"
    _, replay = runner.run_gate(mode="replay", **kwargs)
    assert replay["status"] == "passed"
    assert replay["exact_zero_call_replay"] is True
    assert replay["semantic_changed_paths"] == []
    assert first["installed_runtime"] == replay["installed_runtime"]
    assert events == [("start",), ("activate",), ("map", False), ("finish", "passed", ""), ("map", True)]
    before = runner._gate_snapshot(workspace)
    (site / "auto_zettelkasten" / "__init__.py").write_bytes(b"changed after run")
    with pytest.raises(ValueError, match="installed runtime"):
        runner.run_gate(mode="replay", **kwargs)
    assert len(events) == 5
    assert runner._gate_snapshot(workspace) == before


def test_controlled_pdf_reader_sends_verified_custody_pdf_with_empty_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest(tmp_path / "private")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["cases"] = [payload["cases"][1]]
    payload["gate"] = runner.CONTROLLED_PDF_GATE.manifest_binding()
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    _validated, cases, workspace = runner._validated_manifest(
        manifest,
        sha256_file(manifest),
        runner.CONTROLLED_PDF_GATE,
    )
    case = cases[0]
    helper = _helper_identity()
    events: list[Any] = []

    def fake_status(_self: Any) -> dict[str, Any]:
        events.append("preflight")
        return {
            "version": runner.DIRECT_PDF_CLI_VERSION,
            "helper_version": runner.DIRECT_PDF_CLI_VERSION,
            "helper_manifest_valid": True,
            "pdf_input_file_capability": True,
            "_helper_manifest_identity": helper,
        }

    def fake_read(
        _self: Any,
        text: str,
        metadata: Any,
        question: str | None = None,
        *,
        attachment_paths: Any = (),
    ) -> dict[str, Any]:
        events.append((text, metadata, question, tuple(attachment_paths)))
        return {"accepted": True}

    monkeypatch.setattr(runner.CodexReader, "pdf_input_file_status", fake_status)
    monkeypatch.setattr(runner.CodexReader, "read_source_bundle", fake_read)
    reader = runner._ControlledPdfReader(
        runner.SOURCE_MODEL,
        allow_cloud=True,
        reasoning_effort=runner.REASONING_EFFORT,
        controlled_workspace=workspace,
        controlled_case=case,
        controlled_question="frozen manifest question",
    )
    assert reader.source_question == "frozen manifest question"
    copied_custody_path = case["path"].with_name("A2.pdf")
    copied_custody_path.write_bytes(case["path"].read_bytes())
    metadata = {
        **case["parent"]["data"],
        "_source_context": {
            "source_id": runner.source_id_for_item(case["parent"]),
            "zotero_key": case["parent"]["key"],
            "source_file": str(copied_custody_path),
            "custody_sha256": case["sha256"],
            "route": runner.TEXT_ROUTE,
            "media_type": "application/pdf",
            "source_scope": "full_document",
        },
    }

    assert reader.should_read_source_bundle_directly(
        "adequate embedded text", metadata
    )
    assert reader.read_source_bundle("adequate embedded text", metadata) == {
        "accepted": True
    }
    assert events == [
        "preflight",
        "preflight",
        ("", metadata, "frozen manifest question", (copied_custody_path,)),
    ]

    outside = workspace / "outside.pdf"
    outside.write_bytes(case["path"].read_bytes())
    outside_metadata = json.loads(json.dumps(metadata))
    outside_metadata["_source_context"]["source_file"] = str(outside)
    with pytest.raises(
        runner.ProviderIsolationFailure,
        match="does not match the manifest",
    ):
        reader.should_read_source_bundle_directly("", outside_metadata)


def test_controlled_route_keeps_honest_text_acquisition_and_requires_pdf_transport(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path / "private")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["cases"] = [payload["cases"][1]]
    payload["gate"] = runner.CONTROLLED_PDF_GATE.manifest_binding()
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    validated, cases, workspace = runner._validated_manifest(
        manifest,
        sha256_file(manifest),
        runner.CONTROLLED_PDF_GATE,
    )
    case = cases[0]
    item_root = (
        workspace
        / "11_state"
        / "runs"
        / str(validated["run_id"])
        / "items"
        / str(case["parent"]["key"])
    )
    write_yaml(
        item_root / "frozen_content.yml",
        {
            "content_hash": case["sha256"],
            "source_file": str(case["path"]),
            "content_route": runner.TEXT_ROUTE,
            "media_type": "application/pdf",
            "source_scope": "full_document",
        },
    )

    errors, routes = runner._route_errors(
        workspace,
        str(validated["run_id"]),
        cases,
        runner.CONTROLLED_PDF_GATE,
    )
    assert errors == []
    assert routes == [
        {
            "case_id": case["case_id"],
            "expected_route": runner.PDF_INPUT_ROUTE,
            "acquisition_route": runner.TEXT_ROUTE,
            "selected_pages": [],
            "recovery": "not_applicable",
        }
    ]
    assert runner._direct_pdf_transport_errors(
        cases,
        [
            _usage_row(
                1,
                source=True,
                contract_id="source_bundle",
                pdf_hash=str(case["sha256"]),
            )
        ],
    ) == []
    four_pdf_errors, _routes = runner._route_errors(
        workspace,
        str(validated["run_id"]),
        cases,
        runner.FOUR_PDF_GATE,
    )
    assert f"{case['case_id']}:pdf_input_route_missing" in four_pdf_errors


def test_private_gate_binds_explicit_pdf_fallback(tmp_path: Path) -> None:
    manifest_path = _manifest(tmp_path / "private")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["pdf_fallback"] = "ocr"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    manifest, _cases, workspace = runner._validated_manifest(
        manifest_path, sha256_file(manifest_path)
    )

    assert runner._request(manifest, workspace).extraction_policy.pdf_fallback == "ocr"

    payload["pdf_fallback"] = "automatic"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest pdf_fallback"):
        runner._validated_manifest(manifest_path, sha256_file(manifest_path))


def test_legacy_cli_and_image_route_evidence_remain_accepted(tmp_path: Path) -> None:
    legacy = _usage_row(
        1,
        source=True,
        contract_id="source_bundle",
        cli_version=runner.LEGACY_CODEX_CLI_VERSION,
    )
    assert runner._completion_error(legacy, source=True) == ""

    manifest_path = _manifest(tmp_path / "private")
    manifest, cases, workspace = runner._validated_manifest(
        manifest_path, sha256_file(manifest_path)
    )
    cases[1]["expected_route"] = runner.IMAGE_ROUTE
    cases[1]["expected_selected_pages"] = [1]
    client = runner.ManifestZoteroClient(cases)
    _write_accepted_run(
        workspace,
        runner._request(manifest, workspace),
        client,
        str(manifest["run_id"]),
    )
    settings = runner.GateSettings(
        kind="raw_e2e",
        allow_html=True,
        require_private_expectations=False,
        require_direct_image_route=False,
        require_direct_pdf_route=False,
    )

    errors, _ = runner._route_errors(
        workspace, str(manifest["run_id"]), cases, settings
    )

    assert errors == []


def test_four_pdf_replay_snapshot_covers_every_existing_workspace_artifact(
    tmp_path: Path,
) -> None:
    initialize_workspace(tmp_path)
    custody_ledger = tmp_path / "01_custody" / "read_attempts.jsonl"
    custody_ledger.write_text('{"status":"preserved"}\n', encoding="utf-8")
    evaluation = tmp_path / "11_state" / "evaluations" / "replay.yml"
    evaluation.parent.mkdir(parents=True)
    evaluation.write_text("ignored\n", encoding="utf-8")
    attempt_ledger = tmp_path / runner.FOUR_PDF_GATE.attempt_ledger_name
    attempt_ledger.write_text("ignored\n", encoding="utf-8")

    snapshot = runner._gate_snapshot(tmp_path)

    assert "auto-zettelkasten.yml" in snapshot
    assert "01_custody/read_attempts.jsonl" in snapshot
    assert "11_state/evaluations/replay.yml" in snapshot
    assert runner.FOUR_PDF_GATE.attempt_ledger_name in snapshot


def _preflight(
    dimensions: list[tuple[int, int]], *, direct_pdf: bool = False
) -> dict[str, Any]:
    prompt_tokens = 100
    image_tokens = runner._image_token_estimate(dimensions)
    document_tokens = prompt_tokens + (image_tokens if direct_pdf else 0)
    uncertainty = 16_384
    payload = {
        "document_input_tokens": document_tokens,
        "image_tokens": image_tokens,
        "reasoning_reservation_tokens": 32_768,
        "output_reservation_tokens": 32_768,
        "uncertainty_tokens": uncertainty,
        "combined_tokens": (
            document_tokens
            + (0 if direct_pdf else image_tokens)
            + 32_768
            + 32_768
            + uncertainty
        ),
        "ceiling_tokens": 200_000,
        "admitted": True,
    }
    if direct_pdf:
        payload.update(
            prompt_text_tokens=prompt_tokens,
            pdf_extracted_text_tokens=0,
        )
    return payload


def _write_accepted_run(
    workspace: Path, request: Any, client: Any, run_id: str
) -> dict[str, Any]:
    assert request.provider == "codex"
    assert request.model == runner.SOURCE_MODEL
    assert request.literature_model == runner.RELATIONSHIP_MODEL
    assert request.reasoning_effort == "medium"
    assert request.provider_concurrency == "auto"
    assert request.retry_terminal_failures is False
    assert request.extraction_policy.ocr == "auto"
    assert request.extraction_policy.pdf_fallback == "none"
    assert request.processing.max_calls_per_document_run == 2
    assert request.literature_policy.cluster_generation_enabled is False
    assert request.literature_policy.max_profile_calls == 6
    assert request.literature_policy.max_synthesis_calls == 8
    items = client.inventory("library")
    assert len(items) == 4
    for item in items:
        attachment = client.children(item["key"])[0]
        data, media_type = client.file(attachment["key"])
        assert data.startswith(b"synthetic PDF")
        assert media_type == "application/pdf"

    run_root = workspace / "11_state" / "runs" / run_id
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "inventory.json").write_text(
        json.dumps(items, sort_keys=True), encoding="utf-8"
    )
    note_root = workspace / "02_source_memory" / "notes"
    profile_root = workspace / "02_source_memory" / "profiles"
    source_ids = [runner.source_id_for_item(item) for item in items]
    report_items = []
    for index, source_id in enumerate(source_ids, 1):
        parent_key = f"P{index}"
        case = client._by_parent[parent_key]
        item_root = run_root / "items" / parent_key
        item_root.mkdir(parents=True, exist_ok=True)
        route = str(case["expected_route"])
        write_yaml(
            item_root / "frozen_content.yml",
            {
                "checkpoint_version": "1",
                "text_hash": "0" * 64,
                "captured_at": "synthetic",
                "content_hash": case["sha256"],
                "source_file": str(case["path"]),
                "content_route": route,
                "media_type": "application/pdf",
                "source_scope": "full_document",
            },
        )
        (item_root / "source.txt").write_text("", encoding="utf-8")
        if route == runner.PDF_INPUT_ROUTE:
            dimensions = [(1_024, 1_024)]
            helper = _helper_identity()
            identity = {
                "route_version": "1",
                "route": runner.PDF_INPUT_ROUTE,
                "custody_file": str(case["path"]),
                "custody_sha256": case["sha256"],
                "custody_byte_count": case["path"].stat().st_size,
                "file_policy": {
                    "media_type": "application/pdf",
                    "maximum_bytes_exclusive": 50_000_000,
                    "detail": "auto",
                },
                "model_profile": {
                    "model": runner.SOURCE_MODEL,
                    "reasoning_effort": runner.REASONING_EFFORT,
                    "cli_version": runner.DIRECT_PDF_CLI_VERSION,
                },
                "fallback_policy": "none",
                "attachment_capability": (
                    runner.codex_source_bundle_attachment_identity(
                        runner.DIRECT_PDF_CLI_VERSION, helper
                    )
                ),
                "probe_evidence": {
                    "status": "succeeded",
                    "reason": "synthetic",
                    "custody_byte_count": case["path"].stat().st_size,
                    "page_count": 1,
                    "suspicious_pages": [1],
                    "render_candidate_pages": [1],
                    "pages": [
                        {
                            "page_number": 1,
                            "printed_page": "1",
                            "width": 1_024,
                            "height": 1_024,
                            "embedded_text_sha256": "0" * 64,
                            "embedded_char_count": 0,
                            "embedded_word_count": 0,
                            "text_quality": "empty",
                            "resource_types": ["image"],
                            "resource_count": 1,
                            "xobject_count": 1,
                            "image_count": 1,
                            "suspicious": True,
                            "visually_consequential": True,
                            "render_candidate": True,
                            "error_type": "",
                        }
                    ],
                },
                "projected_preflight": _preflight(dimensions, direct_pdf=True),
            }
            write_yaml(
                item_root / "document_route.yml",
                {
                    "identity_payload": identity,
                    "identity": runner.stable_hash(identity),
                    "recovery": {"state": "not_selected"},
                },
            )
        elif route == runner.IMAGE_ROUTE:
            pages = list(case["expected_selected_pages"])
            dimensions = [(1_024, 1_024)]
            preflight = _preflight(dimensions)
            probe_pages = [
                {
                    "page_number": page_number,
                    "printed_page": str(page_number),
                    "width": 1_024,
                    "height": 1_024,
                    "embedded_text_sha256": "0" * 64,
                    "embedded_char_count": 0,
                    "embedded_word_count": 0,
                    "text_quality": "empty",
                    "resource_types": ["image"],
                    "resource_count": 1,
                    "xobject_count": 1,
                    "image_count": 1,
                    "suspicious": page_number in pages,
                    "visually_consequential": page_number in pages,
                    "render_candidate": page_number in pages,
                    "error_type": "",
                }
                for page_number in range(1, pages[0] + 1)
            ]
            identity = {
                "route_version": "1",
                "route": runner.IMAGE_ROUTE,
                "custody_file": str(case["path"]),
                "custody_sha256": case["sha256"],
                "selected_pages": pages,
                "render_policy": {
                    "format": "png",
                    "maximum_side": 2_048,
                    "maximum_pages": 16,
                    "enlargement": False,
                },
                "attachment_capability": (
                    runner.codex_source_bundle_attachment_identity()
                ),
                "probe_evidence": {
                    "status": "succeeded",
                    "reason": "synthetic",
                    "custody_byte_count": case["path"].stat().st_size,
                    "page_count": len(probe_pages),
                    "suspicious_pages": pages,
                    "render_candidate_pages": pages,
                    "pages": probe_pages,
                },
                "projected_preflight": preflight,
            }
            write_yaml(
                item_root / "document_route.yml",
                {
                    "identity_payload": identity,
                    "identity": runner.stable_hash(identity),
                    "rendered_images": [
                        {
                            "page_number": pages[0],
                            "sha256": f"{index}" * 64,
                            "media_type": "image/png",
                            "width": 1_024,
                            "height": 1_024,
                            "byte_count": 1_024,
                            "renderer": "synthetic",
                            "renderer_version": "1",
                            "render_policy_version": "1",
                        }
                    ],
                    "actual_preflight": preflight,
                    "recovery": {"state": "not_selected"},
                },
            )
        note_path = note_root / f"note-{index}.md"
        related = []
        if index == 1:
            related = [{"note_id": "note-2"}]
        elif index == 2:
            related = [{"note_id": "note-1"}]
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(
            "---\n"
            + f"source_id: {source_id}\n"
            + f"note_id: note-{index}\n"
            + "related_notes: "
            + json.dumps(related)
            + "\n---\n"
            + f"# Synthetic source {index}\n\n"
            + f"Audited locator {index}. Expected answer {index}.\n",
            encoding="utf-8",
        )
        write_yaml(
            profile_root / f"note-{index}.yml",
            {
                "profile_schema_version": "1",
                "profile": {"source_id": source_id, "note_id": f"note-{index}"},
            },
        )
        report_items.append(
            {
                "source_id": source_id,
                "note_id": f"note-{index}",
                "note_path": str(note_path.relative_to(workspace)),
                "terminal_status": "validated_note",
            }
        )

    relation = {
        "relation_id": "synthetic-relation",
        "source_id": source_ids[0],
        "target_source_id": source_ids[1],
        "relation_type": "complements",
        "decision_status": "accepted",
        "active": True,
    }
    registry = {
        "relations": [relation],
        "links": [relation],
        "pair_decisions": [],
        "current_pair_decisions": [
            {"source_ids": source_ids[:2], "status": "accepted"}
        ],
    }
    for name in ("typed_links.yml", "typed_note_links.yml"):
        write_yaml(
            workspace / "02_source_memory" / "indexes" / name,
            registry,
        )
    write_yaml(
        workspace / "02_source_memory" / "indexes" / "relationship_selection_state.yml",
        {
            "relationship_stage_complete": True,
            "relationship_discovery_status": "complete",
            "relationship_discovery_incomplete_jobs": [],
        },
    )
    source_rows = [
        _usage_row(
            index,
            source=True,
            contract_id="source_bundle",
            pdf_hash=(
                str(client._by_parent[f"P{index}"]["sha256"])
                if client._by_parent[f"P{index}"]["expected_route"]
                == runner.PDF_INPUT_ROUTE
                else ""
            ),
        )
        for index in range(1, 5)
    ]
    source_usage = run_root / "literature" / "profiles" / "provider_usage.yml"
    write_yaml(
        source_usage,
        {
            "max_calls": 6,
            "provider_call_count": len(source_rows),
            "attempts": source_rows,
        },
    )
    for row in source_rows:
        append_jsonl(
            source_usage.with_name("provider_events.jsonl"),
            {"event_id": f"reserved-{row['attempt_id']}", "event_type": "reserved"},
        )
        append_jsonl(
            source_usage.with_name("provider_events.jsonl"),
            {"event_id": f"finished-{row['attempt_id']}", "event_type": "finished"},
        )
    relationship_rows = [
        _usage_row(
            1,
            source=False,
            contract_id="relationship_candidate_selection",
        ),
        _usage_row(2, source=False, contract_id="bridge_shard_selection"),
        _usage_row(3, source=False, contract_id="relationship_adjudication"),
    ]
    write_yaml(
        run_root / "literature" / "synthesis" / "provider_usage.yml",
        {
            "max_calls": 8,
            "provider_call_count": len(relationship_rows),
            "attempts": relationship_rows,
        },
    )
    report = {
        "status": "completed",
        "inventory_count": 4,
        "validated_note_count": 4,
        "profile_count": 4,
        "profile_valid_count": 4,
        "profile_excluded_count": 0,
        "items": report_items,
        "cluster_map": {
            "status": "clusters_preserved_not_updated",
            "clusters": [],
            "preserved_clusters": [],
        },
        "gap_map": {
            "status": "clusters_preserved_not_updated",
            "gap_candidates": [],
        },
        "cluster_count": 0,
        "mapped_gap_count": 0,
        "source_provider_call_count": 4,
        "literature_provider_call_count": 3,
        "synthesis_call_count": 3,
        "provider_call_count": 7,
    }
    write_yaml(run_root / "run_report.yml", report)
    return report


def test_hash_and_execute_refusals_precede_map_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (
        runner.run_gate.__kwdefaults__["attempt_guard_factory"].__func__
        is runner.CodexCampaignGuard.start.__func__
    )
    manifest = _manifest(tmp_path / "private")
    called = False

    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        nonlocal called
        called = True
        raise AssertionError("map must not run")

    with pytest.raises(ValueError, match="manifest SHA-256 mismatch"):
        runner.run_gate(
            mode="run",
            manifest_path=manifest,
            manifest_sha256="0" * 64,
            execute=True,
            map_runner=forbidden,
        )
    with pytest.raises(PermissionError, match="execute=True"):
        runner.run_gate(
            mode="run",
            manifest_path=manifest,
            manifest_sha256=sha256_file(manifest),
            map_runner=forbidden,
        )
    with pytest.raises(ValueError, match="does not match git HEAD"):
        runner.run_gate(
            mode="run",
            manifest_path=manifest,
            manifest_sha256=sha256_file(manifest),
            execute=True,
            map_runner=forbidden,
            repository_probe=lambda: ("b" * 40, False),
        )
    with pytest.raises(ValueError, match="clean release worktree"):
        runner.run_gate(
            mode="run",
            manifest_path=manifest,
            manifest_sha256=sha256_file(manifest),
            execute=True,
            map_runner=forbidden,
            repository_probe=lambda: (CODE_COMMIT, True),
        )
    monkeypatch.setattr(runner, "_repository_state", _clean_repo)
    with pytest.raises(ValueError, match="require authorization_path"):
        runner.run_gate(
            mode="run",
            manifest_path=manifest,
            manifest_sha256=sha256_file(manifest),
            execute=True,
            map_runner=forbidden,
            repository_probe=runner._repository_state,
        )
    assert not called


def test_revalidation_allows_only_the_gate_evaluator_to_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = "a" * 40
    head = "b" * 40
    monkeypatch.setattr(runner, "_repository_state", lambda: (head, False))

    def git_run(command: tuple[str, ...], **_kwargs: Any) -> Any:
        if command[1:3] == ("merge-base", "--is-ancestor"):
            return runner.subprocess.CompletedProcess(command, 0, "", "")
        return runner.subprocess.CompletedProcess(
            command,
            0,
            "tools/v030_codex_pdf_eval.py\n"
            "tests/test_v030_codex_pdf_eval.py\n"
            "tools/v030_codex_e2e_eval.py\n"
            "tests/test_v030_codex_e2e_eval.py\n",
            "",
        )

    monkeypatch.setattr(runner.subprocess, "run", git_run)
    assert runner._verify_revalidation_repository(base) == head

    def production_change(command: tuple[str, ...], **_kwargs: Any) -> Any:
        if command[1:3] == ("merge-base", "--is-ancestor"):
            return runner.subprocess.CompletedProcess(command, 0, "", "")
        return runner.subprocess.CompletedProcess(
            command, 0, "src/auto_zettelkasten/pipeline.py\n", ""
        )

    monkeypatch.setattr(runner.subprocess, "run", production_change)
    with pytest.raises(ValueError, match="evaluation-only changes"):
        runner._verify_revalidation_repository(base)

    monkeypatch.setattr(runner, "_repository_state", lambda: (head, True))
    with pytest.raises(ValueError, match="clean release worktree"):
        runner._verify_revalidation_repository(base)
    monkeypatch.setattr(runner, "_repository_state", lambda: (head, False))
    monkeypatch.setattr(
        runner.subprocess, "run",
        lambda command, **kwargs: runner.subprocess.CompletedProcess(
            command, 1 if command[1] == "merge-base" else 0,
            "tools/v030_codex_e2e_eval.py\n", "",
        ),
    )
    with pytest.raises(ValueError, match="evaluation-only changes"):
        runner._verify_revalidation_repository(base)


def test_prepare_rejects_uninitialized_workspace(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path / "private")
    (manifest.parent / "11_state" / "workspace_manifest.yml").unlink()

    with pytest.raises(IncompatibleArtifactSchemaError, match="workspace manifest"):
        runner.run_gate(
            mode="prepare",
            manifest_path=manifest,
            manifest_sha256=sha256_file(manifest),
        )


def test_prepare_run_and_exact_replay_are_provider_free(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path / "private")
    digest = sha256_file(manifest)
    authorization, authorization_sha256 = _authorization(manifest.parent)
    guard_events: list[tuple[Any, ...]] = []
    prepare_path, prepared = runner.run_gate(
        mode="prepare",
        manifest_path=manifest,
        manifest_sha256=digest,
    )
    assert prepared["status"] == "prepared"
    assert prepare_path.stat().st_mode & 0o777 == 0o600

    calls: list[tuple[bool, bool]] = []

    def fake_map(
        request: Any,
        *,
        client: Any,
        run_id: str,
        resume: bool,
        reader: Any = None,
        literature_reasoner: Any = None,
    ) -> Any:
        guarded = isinstance(reader, runner._ReplayCodexReader) and isinstance(
            literature_reasoner, runner._ReplayCodexReader
        )
        calls.append((resume, guarded))
        if not resume:
            assert reader is None and literature_reasoner is None
            return _write_accepted_run(Path(request.workspace), request, client, run_id)
        assert guarded
        dynamically_created = runner.CodexReader(
            runner.SOURCE_MODEL,
            allow_cloud=True,
            reasoning_effort=runner.REASONING_EFFORT,
        )
        assert isinstance(dynamically_created.attempt_guard, CodexAttemptDeny)
        with pytest.raises(CodexAttemptStateError, match="forbidden"):
            reserve_codex_attempt(
                dynamically_created.attempt_guard,
                contract_id="source_bundle",
            )
        return read_yaml(
            Path(request.workspace) / "11_state" / "runs" / run_id / "run_report.yml"
        )

    run_path, first = runner.run_gate(
        mode="run",
        manifest_path=manifest,
        manifest_sha256=digest,
        authorization_path=authorization,
        authorization_sha256=authorization_sha256,
        execute=True,
        map_runner=fake_map,
        repository_probe=_clean_repo,
        attempt_guard_factory=_guard_factory(guard_events),
    )
    assert first["status"] == "passed"
    assert first["source_attempt_count"] == 4
    assert first["relationship_attempt_count"] == 3
    assert first["total_attempt_count"] == 7
    assert first["private_expectation_check_count"] == 12
    assert first["attempt_reservation_state"] == "accepted"
    assert run_path.is_file()
    ledger_path = manifest.parent / runner._ATTEMPT_LEDGER_NAME
    ledger_before = ledger_path.read_bytes()
    ledger_mtime_before = ledger_path.stat().st_mtime_ns
    ledger = json.loads(ledger_before)
    assert ledger["state"] == "accepted"
    assert ledger["source_reserved"] == 6
    assert ledger["relationship_reserved"] == 8
    assert ledger["total_reserved"] == 14

    with pytest.raises(ValueError, match="reservation is already consumed"):
        runner.run_gate(
            mode="run",
            manifest_path=manifest,
            manifest_sha256=digest,
            authorization_path=authorization,
            authorization_sha256=authorization_sha256,
            execute=True,
            map_runner=fake_map,
            repository_probe=_clean_repo,
            attempt_guard_factory=_guard_factory(guard_events),
        )

    replay_path, replay = runner.run_gate(
        mode="replay",
        manifest_path=manifest,
        manifest_sha256=digest,
        execute=True,
        map_runner=fake_map,
        repository_probe=_clean_repo,
    )
    assert calls == [(False, False), (True, True)]
    assert replay["status"] == "passed"
    assert replay["exact_zero_call_replay"] is True
    assert replay["semantic_changed_paths"] == []
    assert replay["semantic_file_count"] > 0
    assert replay_path.is_file()
    assert ledger_path.read_bytes() == ledger_before
    assert ledger_path.stat().st_mtime_ns == ledger_mtime_before
    assert guard_events == [
        (
            "start",
            authorization,
            authorization_sha256,
            runner.ATTEMPT_GUARD_STAGE,
            None,
        ),
        ("activate",),
        ("finish", "passed", ""),
    ]


def test_typed_timeout_pauses_and_resume_uses_the_same_reservation(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path / "private")
    digest = sha256_file(manifest)
    authorization, authorization_sha256 = _authorization(manifest.parent)
    guard_events: list[tuple[Any, ...]] = []
    calls: list[bool] = []

    def fake_map(request: Any, *, client: Any, run_id: str, resume: bool) -> Any:
        calls.append(resume)
        if not resume:
            run_root = Path(request.workspace) / "11_state" / "runs" / run_id
            run_root.mkdir(parents=True)
            (run_root / "inventory.json").write_text("[]\n", encoding="utf-8")
            raise runner.ProviderTimeout("private diagnostic must not be reported")
        return _write_accepted_run(Path(request.workspace), request, client, run_id)

    _, paused = runner.run_gate(
        mode="run",
        manifest_path=manifest,
        manifest_sha256=digest,
        authorization_path=authorization,
        authorization_sha256=authorization_sha256,
        execute=True,
        map_runner=fake_map,
        repository_probe=_clean_repo,
        attempt_guard_factory=_guard_factory(guard_events),
    )
    assert paused["status"] == "paused"
    assert paused["paused_by"] == "timeout"
    assert paused["error_type"] == "ProviderTimeout"
    assert "private diagnostic" not in json.dumps(paused)
    assert (
        json.loads(
            (manifest.parent / runner._ATTEMPT_LEDGER_NAME).read_text(encoding="utf-8")
        )["state"]
        == "paused"
    )

    _, resumed = runner.run_gate(
        mode="resume",
        manifest_path=manifest,
        manifest_sha256=digest,
        authorization_path=authorization,
        authorization_sha256=authorization_sha256,
        execute=True,
        map_runner=fake_map,
        repository_probe=_clean_repo,
        attempt_guard_factory=_guard_factory(guard_events),
    )
    assert calls == [False, True]
    assert resumed["status"] == "passed"
    assert resumed["attempt_reservation_state"] == "accepted"
    assert guard_events == [
        (
            "start",
            authorization,
            authorization_sha256,
            runner.ATTEMPT_GUARD_STAGE,
            None,
        ),
        ("activate",),
        ("finish", "paused", "timeout"),
        (
            "start",
            authorization,
            authorization_sha256,
            runner.ATTEMPT_GUARD_STAGE,
            "timeout",
        ),
        ("activate",),
        ("finish", "passed", ""),
    ]


def test_abandoned_running_reservation_resumes_as_interruption(
    tmp_path: Path,
) -> None:
    manifest_path = _manifest(tmp_path / "private")
    manifest_sha256 = sha256_file(manifest_path)
    authorization, authorization_sha256 = _authorization(manifest_path.parent)
    manifest, cases, workspace = runner._validated_manifest(
        manifest_path, manifest_sha256
    )
    run_id = str(manifest["run_id"])
    run_root = workspace / "11_state" / "runs" / run_id
    run_root.mkdir(parents=True)
    (run_root / "inventory.json").write_text("[]\n", encoding="utf-8")
    runner._begin_attempt_reservation(
        workspace,
        runner._ledger_identity(manifest, manifest_sha256),
        mode="run",
        source_count=0,
        relationship_count=0,
    )
    guard_events: list[tuple[Any, ...]] = []

    def fake_map(
        request: Any, *, client: Any, run_id: str, resume: bool
    ) -> dict[str, Any]:
        assert resume is True
        return _write_accepted_run(Path(request.workspace), request, client, run_id)

    _, report = runner.run_gate(
        mode="resume",
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        authorization_path=authorization,
        authorization_sha256=authorization_sha256,
        execute=True,
        map_runner=fake_map,
        repository_probe=_clean_repo,
        attempt_guard_factory=_guard_factory(guard_events),
    )

    assert len(cases) == 4
    assert report["status"] == "passed"
    assert guard_events == [
        (
            "start",
            authorization,
            authorization_sha256,
            runner.ATTEMPT_GUARD_STAGE,
            "interruption",
        ),
        ("activate",),
        ("finish", "passed", ""),
    ]


def test_acceptance_rejects_relationship_endpoint_outside_four_sources(
    tmp_path: Path,
) -> None:
    manifest_path = _manifest(tmp_path / "private")
    manifest, cases, workspace = runner._validated_manifest(
        manifest_path, sha256_file(manifest_path)
    )
    request = runner._request(manifest, workspace)
    client = runner.ManifestZoteroClient(cases)
    report = _write_accepted_run(workspace, request, client, str(manifest["run_id"]))
    path = workspace / "02_source_memory" / "indexes" / "typed_links.yml"
    registry = read_yaml(path)
    registry["relations"][0]["target_source_id"] = "source-outside-gate"
    write_yaml(path, registry)

    errors, _ = runner._acceptance(workspace, str(manifest["run_id"]), cases, report)

    assert "relationship_endpoint_outside_gate" in errors


def test_acceptance_binds_direct_pdf_route_and_transport_evidence(
    tmp_path: Path,
) -> None:
    manifest_path = _manifest(tmp_path / "private")
    manifest, cases, workspace = runner._validated_manifest(
        manifest_path, sha256_file(manifest_path)
    )
    request = runner._request(manifest, workspace)
    client = runner.ManifestZoteroClient(cases)
    run_id = str(manifest["run_id"])
    report = _write_accepted_run(workspace, request, client, run_id)

    errors, _ = runner._acceptance(workspace, run_id, cases, report)
    assert errors == []

    route_path = (
        workspace
        / "11_state"
        / "runs"
        / run_id
        / "items"
        / "P2"
        / "document_route.yml"
    )
    route_bytes = route_path.read_bytes()
    route = read_yaml(route_path)
    route["identity_payload"]["fallback_policy"] = "images"
    route["identity"] = runner.stable_hash(route["identity_payload"])
    write_yaml(route_path, route)
    route_errors, _ = runner._acceptance(workspace, run_id, cases, report)
    assert "case-2:route_identity_mismatch" in route_errors

    route_path.write_bytes(route_bytes)
    route = read_yaml(route_path)
    route["identity_payload"]["projected_preflight"]["prompt_text_tokens"] += 1
    route["identity"] = runner.stable_hash(route["identity_payload"])
    write_yaml(route_path, route)
    preflight_errors, _ = runner._acceptance(workspace, run_id, cases, report)
    assert "case-2:pdf_input_preflight_not_admitted" in preflight_errors

    route_path.write_bytes(route_bytes)
    usage_path = (
        workspace
        / "11_state"
        / "runs"
        / run_id
        / "literature"
        / "profiles"
        / "provider_usage.yml"
    )
    usage = read_yaml(usage_path)
    usage["attempts"][1]["provider_completion"]["attachment_transport"][
        "adapter_protocol"
    ] = "codex-cli-jsonl-v1"
    write_yaml(usage_path, usage)
    transport_errors, _ = runner._acceptance(workspace, run_id, cases, report)
    assert "direct_pdf_transport_invalid" in transport_errors
    assert "direct_pdf_transport_mismatch" in transport_errors


def test_acceptance_allows_complete_empty_relationship_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = _manifest(tmp_path / "private")
    manifest_sha256 = sha256_file(manifest_path)
    manifest, cases, workspace = runner._validated_manifest(
        manifest_path, manifest_sha256
    )
    request = runner._request(manifest, workspace)
    client = runner.ManifestZoteroClient(cases)
    run_id = str(manifest["run_id"])
    report = _write_accepted_run(workspace, request, client, run_id)
    empty_registry = {
        "relations": [],
        "links": [],
        "pair_decisions": [],
        "current_pair_decisions": [],
    }
    for name in ("typed_links.yml", "typed_note_links.yml"):
        write_yaml(
            workspace / "02_source_memory" / "indexes" / name,
            empty_registry,
        )
    usage_path = (
        workspace
        / "11_state"
        / "runs"
        / run_id
        / "literature"
        / "synthesis"
        / "provider_usage.yml"
    )
    usage = read_yaml(usage_path)
    usage["attempts"] = usage["attempts"][:2]
    usage["provider_call_count"] = 2
    write_yaml(usage_path, usage)
    report.update(
        literature_provider_call_count=2,
        synthesis_call_count=2,
        provider_call_count=6,
    )
    write_yaml(workspace / "11_state" / "runs" / run_id / "run_report.yml", report)

    errors, acceptance = runner._acceptance(workspace, run_id, cases, report)

    assert errors == []
    assert acceptance["relationship_attempt_count"] == 2

    state_path = (
        workspace
        / "02_source_memory"
        / "indexes"
        / "relationship_selection_state.yml"
    )
    state = read_yaml(state_path)
    state["selected_candidates"] = [{"source_ids": ["source-a", "source-b"]}]
    write_yaml(state_path, state)
    selected_errors, _ = runner._acceptance(workspace, run_id, cases, report)
    assert "required_relationship_contracts_missing" in selected_errors
    state["selected_candidates"] = []
    write_yaml(state_path, state)

    identity = runner._ledger_identity(manifest, manifest_sha256)
    runner._begin_attempt_reservation(
        workspace,
        identity,
        mode="run",
        source_count=0,
        relationship_count=0,
    )
    runner._finish_attempt_reservation(
        workspace,
        identity,
        state="failed",
        source_count=4,
        relationship_count=2,
    )
    ledger_path = workspace / runner._ATTEMPT_LEDGER_NAME
    ledger_before = ledger_path.read_bytes()

    def fake_resume(
        root: Path,
        resumed_run_id: str,
        *,
        client: Any,
        reader: Any,
        literature_reasoner: Any,
    ) -> Any:
        del client
        assert root == workspace
        assert resumed_run_id == run_id
        assert isinstance(reader, runner._ReplayCodexReader)
        assert isinstance(literature_reasoner, runner._ReplayCodexReader)
        return read_yaml(
            workspace / "11_state" / "runs" / resumed_run_id / "run_report.yml"
        )

    monkeypatch.setattr(runner, "resume_map", fake_resume)
    _, revalidated = runner.run_gate(
        mode="revalidate",
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        execute=True,
        repository_probe=_clean_repo,
    )

    assert revalidated["status"] == "passed"
    assert revalidated["attempt_reservation_state"] == "failed_preserved"
    assert revalidated["exact_zero_call_replay"] is True
    assert revalidated["semantic_changed_paths"] == []
    assert ledger_path.read_bytes() == ledger_before


def test_cumulative_attempt_history_uses_latest_logical_outcome(tmp_path: Path) -> None:
    manifest_path = _manifest(tmp_path / "private")
    manifest, cases, workspace = runner._validated_manifest(
        manifest_path, sha256_file(manifest_path)
    )
    request = runner._request(manifest, workspace)
    client = runner.ManifestZoteroClient(cases)
    run_id = str(manifest["run_id"])
    report = _write_accepted_run(workspace, request, client, run_id)
    report["historical_provider_failures"] = [{"error_type": "ProviderTimeout"}]
    usage_path = (
        workspace
        / "11_state"
        / "runs"
        / run_id
        / "literature"
        / "synthesis"
        / "provider_usage.yml"
    )
    usage = read_yaml(usage_path)
    completed = {**usage["attempts"][0], "attempt_id": "candidate-2", "attempt": 2}
    failed = {
        "attempt_id": "candidate-1",
        "stage": completed["stage"],
        "key": completed["key"],
        "fingerprint": completed["fingerprint"],
        "attempt": 1,
        "status": "failed",
        "failure_class": "timeout",
        "error_type": "ProviderTimeout",
        "provider_completion": {},
    }
    rows = [failed, completed, *usage["attempts"][1:]]

    def persist() -> tuple[list[str], list[dict[str, Any]]]:
        usage["attempts"] = rows
        usage["provider_call_count"] = len(rows)
        write_yaml(usage_path, usage)
        report.update(
            literature_provider_call_count=len(rows),
            synthesis_call_count=len(rows),
            provider_call_count=4 + len(rows),
        )
        write_yaml(workspace / "11_state" / "runs" / run_id / "run_report.yml", report)
        errors, _ = runner._acceptance(workspace, run_id, cases, report)
        source, relationship = runner._attempts(workspace, run_id)
        return errors, [*source["rows"], *relationship["rows"]]

    errors, attempts = persist()
    assert errors == []
    assert runner._pause_reason(report, attempts) == ""

    failed.update(
        failure_class="isolation",
        error_type="ProviderIsolationFailure",
    )
    errors, attempts = persist()
    assert "unfinished_relationship_attempt" in errors
    assert runner._pause_reason(report, attempts) == ""

    failed.update(failure_class="timeout", error_type="ProviderTimeout")
    rows.append(
        {
            **failed,
            "attempt_id": "candidate-3",
            "attempt": 3,
            "status": "interrupted",
            "failure_class": "transport",
            "error_type": "InterruptedProviderAttempt",
            "transport_kind": "interrupted_process",
            "retry_on_resume": True,
        }
    )
    errors, attempts = persist()
    assert "unfinished_relationship_attempt" in errors
    assert runner._pause_reason(report, attempts) == "interruption"

    rows.append({**completed, "attempt_id": "candidate-4", "attempt": 4})
    errors, attempts = persist()
    assert errors == []
    assert runner._pause_reason(report, attempts) == ""


def test_expected_answer_matching_accepts_one_span_paraphrases() -> None:
    spans = [
        runner._normalized_text(
            "The auxiliary sensor is one small but critical part of the broader "
            "control system."
        ),
        runner._normalized_text(
            "A watchdog is a limited but critical component of fault recovery."
        ),
        runner._normalized_text(
            "The weaker form of checksum validation permits different seed values."
        ),
    ]
    text = runner._normalized_text("\n".join(spans))

    assert runner._answer_matches(text, spans, "part of broader control system")
    assert runner._answer_matches(text, spans, "part of fault recovery")
    assert runner._answer_matches(text, spans, "weaker than checksum validation")
    assert not runner._answer_matches(text, spans, "critical checksum validation")

    performance = runner._normalized_text(
        "Berkshire's compounded annual gains for 1965-2024 were 19.9%."
    )
    assert runner._answer_matches(
        performance,
        [performance],
        "Berkshire compounded annual gain 1965-2024 19.9",
    )


@pytest.mark.parametrize(
    ("body", "matches"),
    [
        ("The relay reset is insufficient on its own.", True),
        ("The relay reset is not sufficient on its own.", True),
        ("The relay reset is sufficient on its own.", False),
        ("The relay reset is not insufficient on its own.", False),
        ("The relay reset is never insufficient on its own.", False),
        ("The relay reset is not entirely insufficient on its own.", False),
        ("The relay reset isn't insufficient on its own.", False),
        ("The relay reset is insufficient.", False),
        ("The relay reset is insufficient\non its own.", False),
    ],
)
def test_expected_answer_insufficiency_preserves_phrase_and_polarity(
    body: str, matches: bool,
) -> None:
    spans = [runner._normalized_text(line) for line in body.splitlines()]

    assert runner._answer_matches(
        runner._normalized_text(body), spans, "not sufficient on its own"
    ) is matches


def test_single_source_gate_has_vacuously_complete_relationship_coverage(
    tmp_path: Path,
) -> None:
    index_root = tmp_path / "02_source_memory" / "indexes"
    index_root.mkdir(parents=True)
    registry = {
        "relations": [],
        "links": [],
        "pair_decisions": [],
        "current_pair_decisions": [],
    }
    primary_path = index_root / "typed_links.yml"
    write_yaml(primary_path, registry)
    write_yaml(index_root / "typed_note_links.yml", registry)
    errors, requires_adjudication = runner._relationship_errors(
        tmp_path, {"source-zotero-only"}
    )
    assert errors == []
    assert requires_adjudication is False

    primary_path.unlink()
    errors, _ = runner._relationship_errors(tmp_path, {"source-zotero-only"})
    assert errors == ["typed_relationship_registry_missing"]
    write_yaml(primary_path, registry)

    nonempty_registry = {**registry, "parked": [{"reason": "pending review"}]}
    write_yaml(primary_path, nonempty_registry)
    write_yaml(index_root / "typed_note_links.yml", nonempty_registry)
    errors, _ = runner._relationship_errors(tmp_path, {"source-zotero-only"})
    assert "relationship_completeness_accounting_failed" in errors
    write_yaml(primary_path, registry)
    write_yaml(index_root / "typed_note_links.yml", registry)

    errors, _ = runner._relationship_errors(
        tmp_path, {"source-zotero-first", "source-zotero-second"}
    )
    assert "relationship_completeness_accounting_failed" in errors

    state_path = index_root / "relationship_selection_state.yml"
    write_yaml(state_path, {})
    errors, _ = runner._relationship_errors(tmp_path, {"source-zotero-only"})
    assert "relationship_completeness_accounting_failed" in errors


def test_private_locator_matching_recognizes_numbered_operative_references() -> None:
    for locator in (
        "Numbered commitments 6–8", "Numbered commitment 7",
        "Operative provisions 6–8", "Provision 7",
    ):
        text = runner._normalized_text(locator + ": operative clauses on the left side.")
        assert runner._locator_matches(text, "clause 7")
        assert not runner._locator_matches(text, "clause 9")
    for locator in ("Numbered commitment 70", "Provisions 70–72", "Budget 7"):
        assert not runner._locator_matches(runner._normalized_text(locator), "clause 7")


@pytest.mark.parametrize("label", ["numbered clauses", "operative provisions"])
def test_private_locator_matching_accepts_numbered_ranges_without_prefix_collisions(
    tmp_path: Path, label: str,
) -> None:
    source_id = "source-zotero-synth001"
    note_path = tmp_path / "synthetic-report.md"
    note_path.write_text(
        "Instrument calibration completed.\n\n"
        f"## Locators\n\nSupplied page image: {label} 1–3.\n\n"
        "The system resumes nominal operation.\n",
        encoding="utf-8",
    )
    errors, passed = runner._private_expectation_errors(
        [
            {
                "case_id": "synthetic-image-page",
                "parent": {"key": "SYNTH001"},
                "expectations": {
                    "audited_facts": {
                        "all_of": ["instrument calibration completed"],
                        "any_of": [],
                    },
                    "audited_locators": {
                        "all_of": [],
                        "any_of": ["Clause 1"],
                    },
                    "expected_answers": {
                        "all_of": [],
                        "any_of": ["resumes nominal operation"],
                    },
                },
            }
        ],
        {source_id: note_path},
    )

    assert errors == []
    assert passed == 3
    assert not runner._locator_matches(
        runner._normalized_text("Page 10 and clause 10"), "page 1"
    )
    assert not runner._locator_matches(
        runner._normalized_text("Page 10 and clause 10"), "clause 1"
    )
    assert runner._locator_matches(runner._normalized_text("p. 1"), "page 1")
    assert runner._locator_matches(runner._normalized_text("pp. 1–3"), "page 2")
    assert not runner._locator_matches(runner._normalized_text("p. 10"), "page 1")
    for text in ("p1", "p 1", "page1"):
        assert not runner._locator_matches(runner._normalized_text(text), "page 1")


def test_acceptance_rejects_malformed_codex_usage_telemetry(tmp_path: Path) -> None:
    manifest_path = _manifest(tmp_path / "private")
    manifest, cases, workspace = runner._validated_manifest(
        manifest_path, sha256_file(manifest_path)
    )
    request = runner._request(manifest, workspace)
    client = runner.ManifestZoteroClient(cases)
    run_id = str(manifest["run_id"])
    report = _write_accepted_run(workspace, request, client, run_id)
    usage_path = (
        workspace
        / "11_state"
        / "runs"
        / run_id
        / "literature"
        / "profiles"
        / "provider_usage.yml"
    )
    usage = read_yaml(usage_path)
    usage["attempts"][0]["provider_completion"]["usage"]["input_tokens"] = True
    write_yaml(usage_path, usage)

    errors, _ = runner._acceptance(workspace, run_id, cases, report)

    assert "provider_usage_invalid" in errors


def test_cluster_acceptance_allows_only_terminal_new_cluster_quarantine(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    registry_path = (
        workspace / "03_literature_synthesis" / "cluster_registry.yml"
    )
    pending_cluster = {
        "cluster_id": "cluster-parked",
        "revision_hash": "a" * 64,
        "source_ids": ["source-a", "source-c"],
    }
    pending_synthesis = {
        "cluster_id": "cluster-parked",
        "status": "partial",
        "parked_for_review": True,
        "quality_errors": ["writer_core_relationship_graph_disconnected"],
    }
    terminal_pending = {
        "cluster_id": "cluster-parked",
        "pending_revision_hash": "a" * 64,
        "last_good_revision_hash": "",
        "refresh_pending_source_ids": ["source-a", "source-c"],
        "cluster": pending_cluster,
        "synthesis": pending_synthesis,
    }
    write_yaml(registry_path, {"pending_revisions": [terminal_pending]})
    report = {
        "cluster_count": 1,
        "synthesized_cluster_count": 1,
        "cluster_map": {
            "clusters": [
                {"cluster_id": "cluster-active", "source_ids": ["source-a", "source-b"]}
            ],
            "unclustered_sources": [{"source_id": "source-c"}],
        },
        "literature_packet": {
            "status": "complete",
            "cluster_ids": ["cluster-active"],
            "parked_cluster_ids": ["cluster-parked"],
            "refresh_pending_cluster_ids": [],
        },
    }

    errors = runner._cluster_errors(
        workspace, {"source-a", "source-b", "source-c"}, report
    )
    assert "cluster_registry_incomplete" not in errors

    invalid_pending: list[Any] = [1]
    for path, value in (
        (("last_good_revision_hash",), "b" * 64),
        (("last_good_revision_hash",), False),
        (("pending_revision_hash",), ""),
        (("cluster_id",), False),
        (("retry_on_resume",), True),
        (("retry_on_resume",), []),
        (("cluster", "source_ids"), 1),
        (("cluster", "source_ids"), ["source-outsider"]),
        (("cluster", "refresh_pending"), True),
        (("cluster", "refresh_pending"), 0),
        (("synthesis", "refresh_pending"), True),
        (("synthesis", "refresh_pending"), ""),
        (("synthesis", "quality_errors"), "malformed"),
    ):
        row = json.loads(json.dumps(terminal_pending))
        target = row
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        invalid_pending.append([row])
    for pending in invalid_pending:
        write_yaml(registry_path, {"pending_revisions": pending})
        errors = runner._cluster_errors(
            workspace, {"source-a", "source-b", "source-c"}, report
        )
        assert "cluster_registry_incomplete" in errors


def test_cluster_accounting_uses_runtime_eligibility_with_typed_exclusion(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    raw_profiles = [
        {"source_id": source_id, "note_id": f"note-{source_id}"}
        for source_id in ("source-a", "source-b")
    ]
    raw_profiles.append(
        {
            "source_id": "source-c",
            "note_id": "note-source-c",
            "context": {"date": "1996"},
            "evidence_anchors": [
                {"finding": "Observed results were reported in 2000 and 2001."}
            ],
        }
    )
    normalized = {
        row["source_id"]: row
        for row in runner.normalize_evidence_profiles(raw_profiles)
    }
    for profile in raw_profiles:
        write_yaml(
            workspace
            / "02_source_memory"
            / "profiles"
            / f"{profile['note_id']}.yml",
            {"profile": profile},
        )
    write_yaml(
        workspace / "03_literature_synthesis" / "coverage_register.yml",
        {
            "source_set_id": "source-set-test",
            "inventory_count": 4,
            "counts": {
                "validated_note": 2,
                "limited_note": 1,
                "duplicate_alias": 0,
                "parked_for_review": 1,
                "partial": 0,
                "pending": 0,
            },
            "records": [
                {
                    "source_id": source_id,
                    "terminal_state": "validated_note",
                }
                for source_id in ("source-a", "source-b")
            ]
            + [
                {
                    "source_id": "source-c",
                    "terminal_state": "limited_note",
                    "exclusion_reason": normalized["source-c"]["exclusion_reason"],
                },
                {
                    "source_id": "source-d",
                    "terminal_state": "parked_for_review",
                    "exclusion_reason": "source_parked_for_review",
                },
            ],
            "status": "complete_with_exclusions",
        },
    )
    write_yaml(
        workspace / "03_literature_synthesis" / "cluster_registry.yml",
        {"pending_revisions": []},
    )
    report = {
        "items": [
            {"source_id": source_id}
            for source_id in ("source-a", "source-b", "source-c", "source-d")
        ],
        "cluster_count": 1,
        "synthesized_cluster_count": 1,
        "cluster_map": {
            "clusters": [
                {
                    "cluster_id": "cluster-active",
                    "source_ids": ["source-a", "source-b"],
                }
            ],
            "unclustered_sources": [],
        },
        "literature_packet": {
            "status": "complete",
            "cluster_ids": ["cluster-active"],
            "parked_cluster_ids": [],
            "refresh_pending_cluster_ids": [],
        },
    }

    errors = runner._cluster_errors(
        workspace, {"source-a", "source-b", "source-c", "source-d"}, report
    )

    assert "cluster_disposition_accounting_failed" not in errors
    assert "cluster_integrity_exclusion_unexplained" not in errors

    coverage = read_yaml(
        workspace / "03_literature_synthesis" / "coverage_register.yml"
    )
    coverage["records"][2]["exclusion_reason"] = ""
    write_yaml(
        workspace / "03_literature_synthesis" / "coverage_register.yml",
        coverage,
    )
    errors = runner._cluster_errors(
        workspace, {"source-a", "source-b", "source-c", "source-d"}, report
    )
    assert "cluster_integrity_exclusion_unexplained" in errors

    coverage["records"][2]["exclusion_reason"] = normalized["source-c"][
        "exclusion_reason"
    ]
    coverage["counts"]["validated_note"] = 3
    coverage["counts"]["limited_note"] = 0
    write_yaml(
        workspace / "03_literature_synthesis" / "coverage_register.yml",
        coverage,
    )
    errors = runner._cluster_errors(
        workspace, {"source-a", "source-b", "source-c", "source-d"}, report
    )
    assert "cluster_coverage_register_invalid" in errors

    coverage["counts"]["validated_note"] = 2
    coverage["counts"]["limited_note"] = 1
    coverage["records"].append(dict(coverage["records"][2]))
    coverage["inventory_count"] = 5
    coverage["counts"]["limited_note"] = 2
    write_yaml(
        workspace / "03_literature_synthesis" / "coverage_register.yml",
        coverage,
    )
    errors = runner._cluster_errors(
        workspace, {"source-a", "source-b", "source-c", "source-d"}, report
    )
    assert "cluster_coverage_register_invalid" in errors

    (
        workspace / "03_literature_synthesis" / "coverage_register.yml"
    ).unlink()
    errors = runner._cluster_errors(
        workspace, {"source-a", "source-b", "source-c", "source-d"}, report
    )
    assert "cluster_coverage_register_invalid" in errors


def test_cluster_acceptance_rejects_retained_and_dropped_member_overlap(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    write_yaml(
        workspace / "03_literature_synthesis" / "cluster_registry.yml",
        {"pending_revisions": []},
    )
    report = {
        "cluster_count": 1,
        "synthesized_cluster_count": 1,
        "cluster_map": {
            "clusters": [
                {
                    "cluster_id": "cluster-active",
                    "source_ids": ["source-a", "source-b"],
                }
            ],
            "unclustered_sources": [],
            "cluster_syntheses": {
                "cluster-active": {
                    "retained_member_ids": ["source-a", "source-b"],
                    "dropped_members": [
                        {"source_id": "source-b", "reason": "stale"}
                    ],
                }
            },
        },
        "literature_packet": {
            "status": "complete",
            "cluster_ids": ["cluster-active"],
            "parked_cluster_ids": [],
            "refresh_pending_cluster_ids": [],
        },
    }

    errors = runner._cluster_errors(
        workspace, {"source-a", "source-b"}, report
    )

    assert "cluster_synthesis_membership_contradiction" in errors
