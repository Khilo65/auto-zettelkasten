from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import stat
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from auto_zettelkasten.files import read_yaml, sha256_file, write_yaml


SPEC = importlib.util.spec_from_file_location(
    "v030_release_quality_audit",
    Path(__file__).parents[1] / "tools/v030_release_quality_audit.py",
)
assert SPEC and SPEC.loader
audit_tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit_tool)


def _run_git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args),
        cwd=repository,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()


def _release_repository(root: Path) -> tuple[Path, Path, Path]:
    repository = root / "repository"
    package = repository / "src" / "auto_zettelkasten"
    package.mkdir(parents=True)
    files = {
        "pyproject.toml": (
            "[project]\nname = \"auto-zettelkasten\"\nversion = \"0.30.0\"\n"
            "[project.scripts]\n"
            "auto-zettelkasten = \"auto_zettelkasten:main\"\n"
        ),
        "README.md": "# Auto-Zettelkasten\n",
        "CHANGELOG.md": "# Changelog\n",
        "LICENSE": "Apache-2.0\n",
        ".gitignore": ".env\nauth.json\n",
        "src/auto_zettelkasten/__init__.py": '__version__ = "0.30.0"\n',
    }
    for relative, content in files.items():
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _run_git(repository.parent, "init", "-q", repository.name)
    _run_git(repository, "config", "user.email", "test@example.invalid")
    _run_git(repository, "config", "user.name", "Test")
    _run_git(repository, "add", ".")
    _run_git(repository, "commit", "-qm", "release fixture")
    sdist, wheel = _write_release_archives(repository, root / "artifacts")
    return repository, sdist, wheel


def _write_release_archives(
    repository: Path,
    destination: Path,
    *,
    wheel_source: bytes | None = None,
    extra_wheel: tuple[str, bytes] | None = None,
    wheel_overrides: dict[str, bytes] | None = None,
    package_info_override: bytes | None = None,
    corrupt_record: bool = False,
    symlink_member: str | None = None,
) -> tuple[Path, Path]:
    destination.mkdir(parents=True, exist_ok=True)
    root = "auto_zettelkasten-0.30.0"
    source_files = {
        path.relative_to(repository).as_posix(): path.read_bytes()
        for path in sorted((repository / "src/auto_zettelkasten").rglob("*.py"))
    }
    wheel_source_files = dict(source_files)
    if wheel_source is not None:
        wheel_source_files["src/auto_zettelkasten/__init__.py"] = wheel_source
    package_info = package_info_override or (
        b"Name: auto-zettelkasten\nVersion: 0.30.0\n"
    )
    sdist_files = {
        f"{root}/{name}": (repository / name).read_bytes()
        for name in ("CHANGELOG.md", "LICENSE", "README.md", "pyproject.toml", ".gitignore")
    }
    sdist_files.update(
        {
            **{f"{root}/{name}": data for name, data in source_files.items()},
            f"{root}/PKG-INFO": package_info,
        }
    )
    sdist = destination / "auto_zettelkasten-0.30.0.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        for name, data in sorted(sdist_files.items()):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))

    dist_info = "auto_zettelkasten-0.30.0.dist-info"
    wheel_files = {
        **{
            name.removeprefix("src/"): data
            for name, data in wheel_source_files.items()
        },
        f"{dist_info}/METADATA": package_info,
        f"{dist_info}/WHEEL": (
            b"Wheel-Version: 1.0\n"
            b"Generator: hatchling 1.32.0\n"
            b"Root-Is-Purelib: true\n"
            b"Tag: py3-none-any\n"
        ),
        f"{dist_info}/entry_points.txt": (
            b"[console_scripts]\nauto-zettelkasten = auto_zettelkasten:main\n"
        ),
        f"{dist_info}/licenses/LICENSE": (repository / "LICENSE").read_bytes(),
    }
    if extra_wheel is not None:
        wheel_files[extra_wheel[0]] = extra_wheel[1]
    if wheel_overrides is not None:
        wheel_files.update(wheel_overrides)
    record = f"{dist_info}/RECORD"
    record_rows = []
    for name, data in sorted(wheel_files.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
        record_rows.append(
            f"{name},sha256={digest.decode('ascii')},{len(data)}\n"
        )
    record_rows.append(f"{record},,\n")
    if corrupt_record:
        first = record_rows[0].split(",", 1)[0]
        record_rows[0] = f"{first},sha256=incorrect,1\n"
    wheel_files[record] = "".join(record_rows).encode()
    wheel = destination / "auto_zettelkasten-0.30.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, data in wheel_files.items():
            if name == symlink_member:
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, data)
            else:
                archive.writestr(name, data)
    return sdist, wheel


def _fake_openai_token() -> str:
    return "s" + "k-" + "A" * 24


def _fake_private_root() -> str:
    return "/" + "Users" + "/private-evidence"


def _fake_github_pat() -> str:
    return "github_" + "pat_" + "A" * 30


def _fake_pypi_token() -> str:
    return "py" + "pi-" + "A" * 30


def _fake_json_private_roots() -> bytes:
    windows = "C:" + chr(92) + "Users" + chr(92) + "Alice" + chr(92) + "secret"
    unix = _fake_private_root().replace("private-evidence", "Alice")
    return (
        json.dumps({"windows": windows, "unix": unix})
        .replace("/", chr(92) + "/")
        .encode()
    )


def _private_workspace_marker() -> str:
    return "Auto-Zettelkasten" + "-test"


def _private_literal_denylist(
    root: Path, entries: list[tuple[str, str]]
) -> tuple[Path, str]:
    path = root / "private" / "PRIVATE_LITERAL_DENYLIST.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "literals": [
            {"label": label, "literal": literal} for label, literal in entries
        ],
        "private_literal_denylist_schema_version": "1",
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
        encoding="ascii",
    )
    return path, sha256_file(path)


def _workspace(
    root: Path,
    *,
    source_count: int = 40,
    accepted_count: int = 20,
    negative_count: int = 20,
    cluster_count: int = 1,
) -> Path:
    workspace = root / "workspace"
    sources = []
    stratum_count = 4 if source_count == 40 else 20
    for index in range(source_count):
        source_id = f"source-{index:03d}"
        note_id = f"note-{index:03d}"
        note_path = workspace / "02_source_memory" / "notes" / f"{note_id}.md"
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(f"# Note {index}\n\nSource-grounded analysis {index}.\n")
        sources.append(
            {
                "source_id": source_id,
                "note_id": note_id,
                "note_path": str(note_path.relative_to(workspace)),
                "primary_stratum_id": f"stratum-{index % stratum_count:02d}",
            }
        )
    write_yaml(
        workspace / "11_state" / "harness_bakeoff_manifest.yml",
        {"source_count": source_count, "sources": sources},
    )
    accepted = []
    negative = []
    for index in range(accepted_count):
        left = index % source_count
        right = (
            left + 1 if source_count == 40 or index < 100 else left + stratum_count
        ) % source_count
        accepted.append(
            {
                "pair_job_id": f"accepted-{index}",
                "left_source_id": f"source-{left:03d}",
                "right_source_id": f"source-{right:03d}",
                "decision_status": "accepted",
                "active": True,
                "relation_type": "complements",
                "left_endpoint_claim": "Left claim",
                "right_endpoint_claim": "Right claim",
            }
        )
    for index in range(negative_count):
        negative.append(
            {
                "pair_job_id": f"negative-{index}",
                "left_source_id": f"source-{index % source_count:03d}",
                "right_source_id": f"source-{(index + 1) % source_count:03d}",
                "decision_status": "no_relationship",
                "active": True,
                "reason": "No defensible relationship",
            }
        )
    accepted.append(
        {
            **accepted[0],
            "pair_job_id": "superseded-accepted",
            "active": False,
        }
    )
    negative.append(
        {
            **negative[0],
            "pair_job_id": "superseded-negative",
            "active": False,
        }
    )
    write_yaml(
        workspace / "02_source_memory" / "indexes" / "typed_links.yml",
        {"pair_decisions": [*accepted, *negative]},
    )
    clusters = []
    syntheses = {}
    size = source_count // cluster_count
    for cluster_index in range(cluster_count):
        source_ids = [
            f"source-{index:03d}"
            for index in range(cluster_index * size, (cluster_index + 1) * size)
        ]
        cluster_id = f"cluster-{cluster_index:02d}"
        clusters.append(
            {
                "cluster_id": cluster_id,
                "label": f"Synthetic cluster {cluster_index}",
                "source_ids": source_ids,
                "source_roles": [
                    {"source_id": source_id, "role": "core"} for source_id in source_ids
                ],
                "guiding_question": "What connects these sources?",
                "status": "source_backed_cluster",
            }
        )
        syntheses[cluster_id] = {
            "cluster_id": cluster_id,
            "status": "reasoned",
            "synthesis": "All central claims are supported.",
        }
    write_yaml(
        workspace / "03_literature_synthesis" / "cluster_registry.yml",
        {"clusters": clusters, "unclustered_sources": []},
    )
    write_yaml(
        workspace / "03_literature_synthesis" / "cluster_syntheses.yml",
        {"syntheses": syntheses},
    )
    write_yaml(
        workspace / "03_literature_synthesis" / "manifest.yml",
        {"map_id": "synthetic-map", "source_count": source_count},
    )
    return workspace


def _exhaustive_inputs(
    root: Path, workspace: Path, *, custody_root: Path | None = None
) -> tuple[Path, Path, Path]:
    baseline = root / "baseline"
    current = read_yaml(workspace / "11_state" / "harness_bakeoff_manifest.yml")
    baseline_sources = []
    custody_cases = []
    custody = custody_root or root / "custody"
    for index, row in enumerate(current["sources"]):
        source_id = row["source_id"]
        metadata_only = index >= 34
        note_status = (
            "metadata_only_source_note" if metadata_only else "analytical_atomic_note"
        )
        source_scope = "metadata_only" if metadata_only else "full_document"
        current_note = workspace / row["note_path"]
        current_note.write_text(
            "---\n"
            f"note_status: {note_status}\n"
            f"source_scope: {source_scope}\n"
            "---\n\n"
            + (
                f"Metadata-only record for {source_id}; full text was not available.\n"
                if metadata_only
                else f"Source-grounded analysis for {source_id}.\n"
            )
        )
        note_path = baseline / row["note_path"]
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(
            "---\n"
            f"note_status: {note_status}\n"
            f"source_scope: {source_scope}\n"
            "---\n\n"
            f"# Prior accepted {source_id}\n\nBounded prior analysis.\n"
        )
        baseline_sources.append(
            {**row, "note_path": str(note_path.relative_to(baseline))}
        )
        case = {
            "case_id": source_id,
            "source_id": source_id,
            "media_type": "application/json" if metadata_only else "text/plain",
            "expected": {
                "content_route": ("zotero_metadata" if metadata_only else "plain_text"),
                "terminal_status": (
                    "limited_note" if metadata_only else "validated_note"
                ),
                "note_status": note_status,
                "source_scope": source_scope,
            },
            "zotero_parent": {
                "key": source_id,
                "data": {"key": source_id, "itemType": "document"},
            },
        }
        if not metadata_only:
            raw_path = custody / "raw" / f"{source_id}.txt"
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(f"Frozen source evidence for {source_id}.\n")
            case.update(
                {
                    "file": str(raw_path.relative_to(custody)),
                    "sha256": sha256_file(raw_path),
                }
            )
        custody_cases.append(case)
    baseline_manifest = baseline / "11_state" / "harness_bakeoff_manifest.yml"
    write_yaml(
        baseline_manifest,
        {"source_count": 40, "sources": baseline_sources},
    )
    custody_manifest = custody / "PRIVATE_MANIFEST.yml"
    write_yaml(custody_manifest, {"schema_version": "1", "cases": custody_cases})
    return baseline, baseline_manifest, custody_manifest


def _completed_review(packet_path: Path, packet: dict) -> dict:
    judgments = []
    packet_sha256 = sha256_file(packet_path)
    for row in packet["rows"]:
        judgment = {
            "review_id": row["review_id"],
            "artifact_sha256": row["artifact_sha256"],
            "row_sha256": row["row_sha256"],
            "packet_sha256": packet_sha256,
            "reviewer_task_id": f"task-{row['review_id']}",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "high",
        }
        for field in row["required_judgments"]:
            judgment[field] = not field.endswith(
                ("material_error", "materially_worse", "severe_overmerge")
            )
        judgment["judgment_sha256"] = audit_tool._digest(judgment)
        judgments.append(judgment)
    return {
        "review_schema_version": "2",
        "evidence_status": "autonomous_provisional",
        "never_production_input": True,
        "packet_sha256": packet_sha256,
        "judgments": judgments,
    }


def _strategic8_workspace(root: Path) -> tuple[Path, Path]:
    workspace = _workspace(
        root, source_count=8, accepted_count=3, negative_count=3, cluster_count=1
    )
    cases = []
    for index in range(8):
        source_id = f"source-{index:03d}"
        note_id = f"note-{index:03d}"
        note_path = workspace / "02_source_memory" / "notes" / f"{note_id}.md"
        note_path.write_text(
            "---\n"
            f"source_id: {source_id}\n"
            f"note_id: {note_id}\n"
            "---\n\n"
            f"# Source-grounded note {index}\n",
            encoding="utf-8",
        )
        source_path = workspace / "01_custody" / "files" / f"source-{index:03d}.html"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text(
            f"<html><body>Frozen source evidence {index}</body></html>",
            encoding="utf-8",
        )
        cases.append(
            {
                "case_id": f"p{index}",
                "source_id": source_id,
                "primary_stratum_id": f"stratum-{index:02d}",
                "zotero_parent": {
                    "key": f"P{index}",
                    "data": {"key": f"P{index}", "title": f"Source {index}"},
                },
                "file": str(source_path.relative_to(workspace)),
                "sha256": sha256_file(source_path),
            }
        )
    manifest_path = workspace / "PRIVATE_MANIFEST.json"
    manifest_path.write_text(
        json.dumps({"schema_version": "1", "cases": cases}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return workspace, manifest_path


def _completed_strategic8_review(packet_path: Path, packet: dict) -> dict:
    packet_sha256 = sha256_file(packet_path)
    judgments = []
    for task_id in ("/root/strategic8-reviewer-a", "strategic8-reviewer-b"):
        for row in packet["rows"]:
            judgment = {
                "review_id": row["review_id"],
                "artifact_sha256": row["artifact_sha256"],
                "row_sha256": row["row_sha256"],
                "packet_sha256": packet_sha256,
                "reviewer_task_id": task_id,
                "model": "gpt-5.6-sol",
                "reasoning_effort": "high",
            }
            for field in row["required_judgments"]:
                judgment[field] = field not in {"material_error", "severe_overmerge"}
            judgment["judgment_sha256"] = audit_tool._digest(judgment)
            judgments.append(judgment)
    return {
        "review_schema_version": "2",
        "evidence_status": "autonomous_provisional",
        "never_production_input": True,
        "packet_sha256": packet_sha256,
        "judgments": judgments,
    }


def test_strategic8_packet_requires_two_independent_full_reviewers(
    tmp_path: Path,
) -> None:
    workspace, manifest_path = _strategic8_workspace(tmp_path)
    private = tmp_path / "private-strategic8-review"
    packet_path = private / "packet.yml"
    packet = audit_tool.prepare(
        workspace,
        "strategic8",
        packet_path,
        source_manifest_path=manifest_path,
    )
    assert packet["selection_counts"] == {
        "notes": 8,
        "relationships": 3,
        "memberships": 8,
        "clusters": 1,
        "rejected_or_unclustered": 3,
        "syntheses": 1,
        "total": 24,
    }
    assert all(
        source["source_artifact"]["sha256"]
        for source in packet["source_context"]
    )
    assert all("metadata_diagnostics" not in source for source in packet["source_context"])

    review_path = private / "review.yml"
    report_path = private / "report.yml"
    review = _completed_strategic8_review(packet_path, packet)
    write_yaml(review_path, review)
    report = audit_tool.score(workspace, packet_path, review_path, report_path)
    assert report["status"] == "passed"
    assert report["metrics"]["judgment_count"] == 48
    assert len(report["reviewers"]) == 2

    missing_path = private / "missing-reviewer.yml"
    write_yaml(
        missing_path,
        {**review, "judgments": review["judgments"][: len(packet["rows"])]},
    )
    with pytest.raises(ValueError, match="two independent full reviewer tasks"):
        audit_tool.score(
            workspace, packet_path, missing_path, private / "missing-report.yml"
        )

    disagreement_path = private / "disagreement.yml"
    disagreement = _completed_strategic8_review(packet_path, packet)
    disagreement["judgments"][-1]["pass"] = False
    disagreement["judgments"][-1].pop("judgment_sha256")
    disagreement["judgments"][-1]["judgment_sha256"] = audit_tool._digest(
        disagreement["judgments"][-1]
    )
    write_yaml(disagreement_path, disagreement)
    failed = audit_tool.score(
        workspace,
        packet_path,
        disagreement_path,
        private / "disagreement-report.yml",
    )
    assert failed["status"] == "failed"
    assert failed["checks"]["no_reviewer_disagreements"] is False


def test_strategic8_metadata_diagnostics_match_imported_keys_and_are_hash_bound(
    tmp_path: Path,
) -> None:
    workspace, manifest_path = _strategic8_workspace(tmp_path)
    manifest = json.loads(manifest_path.read_text())
    manifest["cases"][0]["zotero_parent"]["data"]["key"] = "IGNORED-NESTED-KEY"
    manifest["cases"][0]["zotero_parent"]["data"]["date"] = "2025"
    del manifest["cases"][1]["zotero_parent"]["key"]
    manifest_path.write_text(json.dumps(manifest))
    issue_path = workspace / "01_custody" / "zotero" / "zotero_metadata_issues.yml"
    date_issue = {
        "issue_id": "date-review", "zotero_item_key": "P0",
        "current_metadata": {"date": "2025"},
        "recommended_correction": {"date": {"current": "2025", "observed": "2024"}},
        "evidence": {"date": "2024"}, "status": "open",
        "ambiguity": "Document-body identity is diagnostic; Zotero remains canonical.",
    }
    duplicate_issue = {
        "issue_id": "duplicate-review", "zotero_item_keys": ["P0", "P1"],
        "issue_types": ["duplicate_zotero_work"], "status": "open",
    }
    write_yaml(issue_path, {
        "zotero_metadata_issue_schema_version": "1",
        "issues": [date_issue, duplicate_issue, *(
            {"issue_id": key, "zotero_item_key": key}
            for key in ("source-000", "IGNORED-NESTED-KEY", "OUTSIDE-SAMPLE")
        )],
    })
    packet_path = tmp_path / "private-review" / "packet.yml"
    packet = audit_tool.prepare(workspace, "strategic8", packet_path)
    notes = {
        row["payload"]["source_id"]: row["payload"]
        for row in packet["rows"] if row["kind"] == "note"
    }
    evidence = notes["source-000"]["metadata_diagnostics"]
    assert notes["source-000"]["source_metadata"]["data"]["date"] == "2025"
    assert json.loads(manifest_path.read_text()) == manifest
    assert evidence["issues"] == [date_issue, duplicate_issue]
    assert notes["source-001"]["metadata_diagnostics"]["issues"] == [duplicate_issue]
    assert all("metadata_diagnostics" not in notes[f"source-{index:03d}"] for index in range(2, 8))
    assert evidence["artifact"] == {
        "path": "01_custody/zotero/zotero_metadata_issues.yml",
        "path_scope": "workspace", "sha256": sha256_file(issue_path),
    }
    assert packet["artifacts"].count(evidence["artifact"]) == 1
    audit_tool._verify_packet(workspace, packet)
    issue_path.write_text(issue_path.read_text().replace("observed: '2024'", "observed: '2023'"))
    assert sha256_file(issue_path) != evidence["artifact"]["sha256"]
    with pytest.raises(ValueError, match="stale review artifact"):
        audit_tool._verify_packet(workspace, packet)


@pytest.mark.parametrize("issues", ["not a list", ["not a mapping"], [{"zotero_item_keys": "P0"}]])
def test_strategic8_rejects_malformed_metadata_diagnostics(tmp_path: Path, issues: object) -> None:
    workspace, _ = _strategic8_workspace(tmp_path)
    write_yaml(workspace / "01_custody" / "zotero" / "zotero_metadata_issues.yml", {
        "issues": issues,
    })
    with pytest.raises(ValueError, match="metadata diagnostic"):
        audit_tool.prepare(workspace, "strategic8", tmp_path / "private-review" / "packet.yml")


def test_release_quality_packet_is_deterministic_private_and_stale_safe(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    baseline, baseline_manifest, custody_manifest = _exhaustive_inputs(
        tmp_path, workspace
    )
    write_yaml(workspace / "01_custody" / "zotero" / "zotero_metadata_issues.yml", {
        "current_only_diagnostic": "not part of blinded A/B evidence",
    })
    private = tmp_path / "private-review"
    first_path = private / "packet-one.yml"
    second_path = private / "packet-two.yml"
    first_bindings = private / "bindings-one.yml"
    second_bindings = private / "bindings-two.yml"

    with pytest.raises(ValueError, match="requires baseline workspace/manifest"):
        audit_tool.prepare(workspace, "exhaustive40", private / "incomplete.yml")

    first = audit_tool.prepare(
        workspace,
        "exhaustive40",
        first_path,
        baseline_workspace=baseline,
        baseline_manifest_path=baseline_manifest,
        custody_manifest_path=custody_manifest,
        bindings_path=first_bindings,
    )
    second = audit_tool.prepare(
        workspace,
        "exhaustive40",
        second_path,
        baseline_workspace=baseline,
        baseline_manifest_path=baseline_manifest,
        custody_manifest_path=custody_manifest,
        bindings_path=second_bindings,
    )

    assert first == second == read_yaml(first_path)
    assert stat.S_IMODE(first_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(first_bindings.stat().st_mode) == 0o600
    assert first["evidence_status"] == "autonomous_provisional"
    assert first["never_production_input"] is True
    assert first["provider_calls"] == 0
    assert first["selection_counts"] == {
        "notes": 40,
        "relationships": 20,
        "memberships": 40,
        "clusters": 1,
        "rejected_or_unclustered": 20,
        "syntheses": 1,
        "total": 122,
    }
    assert all(row["artifact_sha256"] and row["row_sha256"] for row in first["rows"])
    assert not any("zotero_metadata_issues" in row["path"] for row in first["artifacts"])
    assert any(
        artifact["path"] == "03_literature_synthesis/manifest.yml"
        for artifact in first["artifacts"]
    )
    note = next(row for row in first["rows"] if row["kind"] == "note")
    assert set(note["payload"]["variants"]) == {"A", "B"}
    assert all(
        set(value) == {"artifact_sha256", "note_text"}
        for value in note["payload"]["variants"].values()
    )
    assert {
        "variant_a_identity_correct",
        "variant_a_custody_link_correct",
        "variant_a_status_correct",
        "variant_a_metadata_only_non_pretense",
        "variant_a_locators_supported",
        "variant_a_unsupported_claims_absent",
        "variant_a_false_quotations_absent",
        "variant_b_identity_correct",
    } <= set(note["required_judgments"])
    binding_payload = read_yaml(first_bindings)
    assert binding_payload == read_yaml(second_bindings)
    assert binding_payload["protected_evidence_roots"] == {
        "baseline": str(baseline.resolve()),
        "custody": str(custody_manifest.parent.resolve()),
    }
    note_rows = [row for row in first["rows"] if row["kind"] == "note"]
    status_expectations = [
        row["payload"]["source_evidence"]["status_expectation"] for row in note_rows
    ]
    assert (
        sum(row["terminal_status"] == "validated_note" for row in status_expectations)
        == 34
    )
    assert (
        sum(row["terminal_status"] == "limited_note" for row in status_expectations)
        == 6
    )
    assert all(
        row["note_status"] == "metadata_only_source_note"
        and row["source_scope"] == "metadata_only"
        for row in status_expectations
        if row["metadata_only"]
    )

    review_path = private / "review.yml"
    report_path = private / "report.yml"
    write_yaml(review_path, _completed_review(first_path, first))
    report = audit_tool.score(
        workspace,
        first_path,
        review_path,
        report_path,
        bindings_path=first_bindings,
    )
    assert report["status"] == "passed"
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600
    assert report["metrics"]["material_error_count"] == 0
    assert report["metrics"]["relationship_correctness"]["wilson_95_lower"] >= 0.80
    assert report["metrics"]["membership_correctness"]["wilson_95_lower"] >= 0.80
    assert len(report["judgment_sha256"]) == first["selection_counts"]["total"]
    assert all(
        reviewer["model"] == "gpt-5.6-sol"
        and reviewer["reasoning_effort"] == "high"
        for reviewer in report["reviewers"]
    )
    assert "relationship_wilson_lower_at_least_0_80" not in report["checks"]
    assert report["checks"]["all_current_note_statuses_correct"] is True
    assert report["checks"]["all_metadata_only_notes_non_pretending"] is True
    assert report["metrics"]["metadata_only_non_pretense"]["reviewed"] == 6

    failed_review_path = private / "failed-review.yml"
    failed_report_path = private / "failed-report.yml"
    failed_review = _completed_review(first_path, first)
    limited_row = next(
        row
        for row in note_rows
        if row["payload"]["source_evidence"]["status_expectation"]["metadata_only"]
    )
    binding = next(
        row
        for row in binding_payload["bindings"]
        if row["review_id"] == limited_row["review_id"]
    )
    failed_judgment = next(
        row
        for row in failed_review["judgments"]
        if row["review_id"] == limited_row["review_id"]
    )
    current_prefix = f"variant_{binding['current_variant'].casefold()}_"
    failed_judgment[current_prefix + "status_correct"] = False
    failed_judgment[current_prefix + "metadata_only_non_pretense"] = False
    failed_judgment.pop("judgment_sha256")
    failed_judgment["judgment_sha256"] = audit_tool._digest(failed_judgment)
    write_yaml(failed_review_path, failed_review)
    failed_report = audit_tool.score(
        workspace,
        first_path,
        failed_review_path,
        failed_report_path,
        bindings_path=first_bindings,
    )
    assert failed_report["status"] == "failed"
    assert failed_report["checks"]["all_metadata_only_notes_non_pretending"] is False
    assert failed_report["checks"]["all_current_note_statuses_correct"] is False

    stale_review_path = private / "stale-review.yml"
    stale_review = _completed_review(first_path, first)
    stale_review["judgments"][0]["reviewer_task_id"] = "different-task"
    write_yaml(stale_review_path, stale_review)
    with pytest.raises(ValueError, match="stale or bound"):
        audit_tool.score(
            workspace,
            first_path,
            stale_review_path,
            private / "stale-report.yml",
            bindings_path=first_bindings,
        )

    wrong_model_path = private / "wrong-model.yml"
    wrong_model = _completed_review(first_path, first)
    wrong_model["judgments"][0]["model"] = "gpt-5.6-terra"
    wrong_model["judgments"][0].pop("judgment_sha256")
    wrong_model["judgments"][0]["judgment_sha256"] = audit_tool._digest(
        wrong_model["judgments"][0]
    )
    write_yaml(wrong_model_path, wrong_model)
    with pytest.raises(ValueError, match="must use gpt-5.6-sol"):
        audit_tool.score(
            workspace,
            first_path,
            wrong_model_path,
            private / "wrong-model-report.yml",
            bindings_path=first_bindings,
        )

    shared_note_path = private / "shared-note-reviewer.yml"
    shared_note = _completed_review(first_path, first)
    note_judgment = next(
        judgment
        for judgment in shared_note["judgments"]
        if judgment["review_id"] == note_rows[0]["review_id"]
    )
    other_judgment = next(
        judgment
        for judgment in shared_note["judgments"]
        if judgment["review_id"] != note_judgment["review_id"]
    )
    other_judgment["reviewer_task_id"] = note_judgment["reviewer_task_id"]
    other_judgment.pop("judgment_sha256")
    other_judgment["judgment_sha256"] = audit_tool._digest(other_judgment)
    write_yaml(shared_note_path, shared_note)
    with pytest.raises(ValueError, match="note requires its own reviewer task"):
        audit_tool.score(
            workspace,
            first_path,
            shared_note_path,
            private / "shared-note-report.yml",
            bindings_path=first_bindings,
        )

    protected_packet = baseline / "review-packet.yml"
    protected_bindings = custody_manifest.parent / "review-bindings.yml"
    protected_review = baseline / "review.yml"
    protected_report = custody_manifest.parent / "report.yml"
    protected_packet.write_bytes(first_path.read_bytes())
    protected_bindings.write_bytes(first_bindings.read_bytes())
    protected_review.write_bytes(review_path.read_bytes())
    protected_cases = (
        (protected_packet, first_bindings, review_path, report_path),
        (first_path, protected_bindings, review_path, report_path),
        (first_path, first_bindings, protected_review, report_path),
        (first_path, first_bindings, review_path, protected_report),
    )
    for (
        packet_candidate,
        bindings_candidate,
        review_candidate,
        report_candidate,
    ) in protected_cases:
        with pytest.raises(ValueError, match="outside every evidence workspace/root"):
            audit_tool.score(
                workspace,
                packet_candidate,
                review_candidate,
                report_candidate,
                bindings_path=bindings_candidate,
            )

    baseline_note = next((baseline / "02_source_memory" / "notes").glob("*.md"))
    baseline_text = baseline_note.read_text()
    baseline_note.write_text(baseline_text + "changed\n")
    with pytest.raises(ValueError, match="blinded note artifact binding is stale"):
        audit_tool.score(
            workspace,
            first_path,
            review_path,
            report_path,
            bindings_path=first_bindings,
        )
    baseline_note.write_text(baseline_text)

    typed_path = workspace / "02_source_memory" / "indexes" / "typed_links.yml"
    typed = read_yaml(typed_path)
    typed["changed_after_review"] = True
    write_yaml(typed_path, typed)
    with pytest.raises(ValueError, match="stale review artifact"):
        audit_tool.score(
            workspace,
            first_path,
            review_path,
            report_path,
            bindings_path=first_bindings,
        )
    with pytest.raises(ValueError, match="must be distinct private paths"):
        audit_tool.score(
            workspace,
            first_path,
            review_path,
            first_path,
            bindings_path=first_bindings,
        )
    with pytest.raises(ValueError, match="outside the workspace and Git"):
        audit_tool.prepare(
            workspace,
            "exhaustive40",
            workspace / "packet.yml",
            baseline_workspace=baseline,
            baseline_manifest_path=baseline_manifest,
            custody_manifest_path=custody_manifest,
            bindings_path=private / "inside-rejection-bindings.yml",
        )


def test_stratified_500_packet_preserves_mandatory_rows_above_review_caps(
    tmp_path: Path,
) -> None:
    workspace = _workspace(
        tmp_path,
        source_count=500,
        accepted_count=500,
        negative_count=120,
        cluster_count=20,
    )
    typed_path = workspace / "02_source_memory" / "indexes" / "typed_links.yml"
    typed = read_yaml(typed_path)
    for index, decision in enumerate(typed["pair_decisions"][:220]):
        decision["right_source_id"] = f"source-{(index + 1) % 500:03d}"
    write_yaml(typed_path, typed)
    packet_path = tmp_path / "private" / "packet.yml"
    packet = audit_tool.prepare(workspace, "stratified500", packet_path)

    assert packet["source_count"] == 500
    assert packet["selection_counts"] == {
        "notes": 0,
        "relationships": 420,
        "memberships": 500,
        "clusters": 20,
        "rejected_or_unclustered": 100,
        "syntheses": 20,
        "total": 1060,
    }
    assert packet["sampling_counts"] == {
        "relationships": {"mandatory": 220, "sampled": 200},
        "memberships": {"mandatory": 500, "sampled": 0},
        "clusters": {"mandatory": 20, "sampled": 0},
        "syntheses": {"mandatory": 20, "sampled": 0},
        "rejected_or_unclustered": {"mandatory": 0, "sampled": 100},
        "membership_coverage_additions": 0,
    }
    second_path = tmp_path / "private" / "packet-again.yml"
    assert audit_tool.prepare(workspace, "stratified500", second_path) == packet
    assert second_path.read_bytes() == packet_path.read_bytes()
    relation_ids = {
        row["payload"]["relationship"]["pair_job_id"]
        for row in packet["rows"]
        if row["kind"] == "relationship"
    }
    assert {f"accepted-{index}" for index in range(220)} <= relation_ids
    membership_strata = {
        row["payload"]["member_source_id"].split("-")[-1]
        for row in packet["rows"]
        if row["kind"] == "membership"
    }
    assert len(membership_strata) == 500
    review_path = tmp_path / "private" / "review.yml"
    report_path = tmp_path / "private" / "report.yml"
    write_yaml(review_path, _completed_review(packet_path, packet))
    report = audit_tool.score(workspace, packet_path, review_path, report_path)
    assert report["status"] == "passed"
    assert report["checks"]["relationship_wilson_lower_at_least_0_80"] is True
    assert report["checks"]["membership_wilson_lower_at_least_0_80"] is True

    oversized_path = tmp_path / "private" / "oversized-review.yml"
    oversized = _completed_review(packet_path, packet)
    for judgment in oversized["judgments"][:21]:
        judgment["reviewer_task_id"] = "task-oversized"
        judgment.pop("judgment_sha256")
        judgment["judgment_sha256"] = audit_tool._digest(judgment)
    write_yaml(oversized_path, oversized)
    with pytest.raises(ValueError, match="at most 20 rows"):
        audit_tool.score(
            workspace,
            packet_path,
            oversized_path,
            tmp_path / "private" / "oversized-report.yml",
        )

    old_policy = dict(packet)
    old_policy["selection_policy_revision"] = "capped-v1"
    old_policy.pop("packet_identity")
    old_policy["packet_identity"] = audit_tool._digest(old_policy)
    with pytest.raises(ValueError, match="sampling policy is stale"):
        audit_tool._verify_packet(workspace, old_policy)
    changed_selection = dict(packet)
    changed_selection["selection_identity"] = "0" * 64
    changed_selection.pop("packet_identity")
    changed_selection["packet_identity"] = audit_tool._digest(changed_selection)
    with pytest.raises(ValueError, match="selection identity is invalid"):
        audit_tool._verify_packet(workspace, changed_selection)


def test_stratified_500_sampling_preserves_every_selected_cluster_membership(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path, source_count=500)
    cluster_path = workspace / "03_literature_synthesis" / "cluster_registry.yml"
    synthesis_path = workspace / "03_literature_synthesis" / "cluster_syntheses.yml"
    original = read_yaml(cluster_path)["clusters"][0]
    source_ids = ["source-000", "source-020"]
    clusters = [
        {
            **original,
            "cluster_id": f"cluster-{index:04d}",
            "source_ids": source_ids,
            "source_roles": {source_id: "core" for source_id in source_ids},
        }
        for index in range(801)
    ]
    write_yaml(cluster_path, {"clusters": clusters, "unclustered_sources": []})
    syntheses = {
        cluster["cluster_id"]: {"status": "reasoned", "synthesis": "Grounded claim."}
        for cluster in clusters
    }
    write_yaml(synthesis_path, {"syntheses": syntheses})
    packet_path = tmp_path / "private" / "packet.yml"
    packet = audit_tool.prepare(workspace, "stratified500", packet_path)
    assert packet["sampling_counts"]["clusters"] == {"mandatory": 0, "sampled": 201}
    assert packet["selection_counts"]["syntheses"] == 201
    selected = {
        row["payload"]["cluster"]["cluster_id"]
        for row in packet["rows"]
        if row["kind"] == "cluster"
    }
    memberships = [row for row in packet["rows"] if row["kind"] == "membership"]
    assert {
        row["payload"]["cluster"]["cluster_id"] for row in memberships
    } == selected
    assert len(memberships) == (
        200 + packet["sampling_counts"]["membership_coverage_additions"]
    )
    assert len(memberships) >= 201
    selected_again, mandatory = audit_tool._select_clusters(
        list(reversed(clusters)),
        syntheses,
        {source_id: "same-stratum" for source_id in source_ids},
        exhaustive=False,
    )
    assert not mandatory
    assert {cluster["cluster_id"] for cluster in selected_again} == selected


def test_exhaustive_packet_accepts_custody_manifest_at_workspace_root(
    tmp_path: Path,
) -> None:
    case_root = tmp_path / "same-workspace-custody"
    workspace = _workspace(case_root)
    baseline, baseline_manifest, custody_manifest = _exhaustive_inputs(
        case_root,
        workspace,
        custody_root=workspace,
    )
    private = tmp_path / "same-workspace-private-review"
    packet_path = private / "packet.yml"
    bindings_path = private / "bindings.yml"
    review_path = private / "review.yml"
    report_path = private / "report.yml"

    packet = audit_tool.prepare(
        workspace,
        "exhaustive40",
        packet_path,
        baseline_workspace=baseline,
        baseline_manifest_path=baseline_manifest,
        custody_manifest_path=custody_manifest,
        bindings_path=bindings_path,
    )
    bindings = read_yaml(bindings_path)
    assert bindings["protected_evidence_roots"]["custody"] == str(workspace.resolve())
    write_yaml(review_path, _completed_review(packet_path, packet))
    report = audit_tool.score(
        workspace,
        packet_path,
        review_path,
        report_path,
        bindings_path=bindings_path,
    )
    assert report["status"] == "passed"


def test_exhaustive_packet_accepts_strategic40_json_and_custody_sources(
    tmp_path: Path,
) -> None:
    case_root = tmp_path / "strategic40"
    workspace = _workspace(case_root)
    baseline, baseline_manifest, legacy_custody = _exhaustive_inputs(case_root, workspace)
    legacy_cases = read_yaml(legacy_custody)["cases"]
    for index, case in enumerate(legacy_cases):
        source_id = case["source_id"]
        note_path = workspace / "02_source_memory" / "notes" / f"note-{index:03d}.md"
        note_path.write_text(
            "---\n"
            f"source_id: {source_id}\n"
            f"note_id: note-{index:03d}\n"
            f"note_status: {case['expected']['note_status']}\n"
            f"source_scope: {case['expected']['source_scope']}\n"
            "---\n\n"
            f"Source-grounded analysis for {source_id}.\n",
            encoding="utf-8",
        )
    source_manifest = workspace / "PRIVATE_MANIFEST.json"
    source_manifest.write_text(
        json.dumps({
            "schema_version": "1",
            "cases": [
                {
                    "case_id": case["case_id"],
                    "source_id": case["source_id"],
                    "zotero_parent": case["zotero_parent"],
                }
                for case in legacy_cases
            ],
        }, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    custody_sources = []
    for case in legacy_cases:
        metadata_only = case["expected"]["terminal_status"] == "limited_note"
        raw = None
        if not metadata_only:
            source_path = legacy_custody.parent / case["file"]
            raw = {
                "attachment_key": f"attachment-{case['source_id']}",
                "media_type": case["media_type"],
                "path": case["file"],
                "sha256": sha256_file(source_path),
                "size": source_path.stat().st_size,
            }
        custody_sources.append({
            "parent_key": case["case_id"],
            "parent_record": case["zotero_parent"],
            "source_id": case["source_id"],
            "disposition": "metadata_only" if metadata_only else "substantive_raw_source",
            "raw": raw,
            "selected": {
                "media_type": case["media_type"],
                "route": case["expected"]["content_route"],
                "scope": case["expected"]["source_scope"],
                "terminal_status": case["expected"]["terminal_status"],
            },
        })
    custody_manifest = legacy_custody.parent / "PRIVATE_CUSTODY_MANIFEST.json"
    custody_manifest.write_text(
        json.dumps({"schema_version": 1, "sources": custody_sources}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    private = tmp_path / "private-review"
    packet_path = private / "packet.yml"
    bindings_path = private / "bindings.yml"

    packet = audit_tool.prepare(
        workspace,
        "exhaustive40",
        packet_path,
        source_manifest_path=source_manifest,
        baseline_workspace=baseline,
        baseline_manifest_path=baseline_manifest,
        custody_manifest_path=custody_manifest,
        bindings_path=bindings_path,
    )

    assert packet["selection_counts"]["notes"] == 40
    assert any(
        artifact["path"] == "PRIVATE_MANIFEST.json"
        for artifact in packet["artifacts"]
    )
    assert read_yaml(bindings_path)["custody_manifest_artifact"]["path"].endswith(
        "PRIVATE_CUSTODY_MANIFEST.json"
    )


def test_package_audit_accepts_clean_sdist_built_wheel_and_artifact(
    tmp_path: Path,
) -> None:
    repository, sdist, wheel = _release_repository(tmp_path)
    artifact = tmp_path / "release" / "launch-checklist.yml"
    artifact.parent.mkdir()
    artifact.write_text("status: passed\n", encoding="utf-8")
    report_path = tmp_path / "private" / "package-audit.yml"

    report = audit_tool.package_audit(
        repository,
        sdist,
        wheel,
        report_path,
        base_ref="HEAD",
        artifacts=(artifact,),
    )

    assert report == read_yaml(report_path)
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600
    assert report["status"] == "passed"
    assert report["provider_calls"] == 0
    assert report["distribution"] == "auto-zettelkasten"
    assert report["version"] == "0.30.0"
    assert report["sdist_member_count"] == 7
    assert report["wheel_member_count"] == 6
    assert report["findings"] == []
    assert report["release_artifacts"] == [
        {"path": str(artifact.resolve()), "sha256": sha256_file(artifact)}
    ]


def test_private_denylist_catches_deleted_history_without_disclosing_literals(
    tmp_path: Path,
) -> None:
    repository, sdist, wheel = _release_repository(tmp_path)
    base = _run_git(repository, "rev-parse", "HEAD")
    locator = "PRIVATELOCATOR42"
    phrase = "private audited answer phrase"
    historical = repository / "docs" / f"{locator}.txt"
    historical.parent.mkdir()
    historical.write_text(phrase + "\n", encoding="utf-8")
    _run_git(repository, "add", str(historical.relative_to(repository)))
    _run_git(repository, "commit", "-qm", f"add {phrase}")
    historical.unlink()
    _run_git(repository, "add", "-u")
    _run_git(repository, "commit", "-qm", "remove private fixture")
    denylist, denylist_sha256 = _private_literal_denylist(
        tmp_path,
        [("locator", locator), ("expected-answer", phrase)],
    )
    report_path = tmp_path / "private-report" / "history.yml"

    report = audit_tool.package_audit(
        repository,
        sdist,
        wheel,
        report_path,
        base_ref=base,
        private_denylist_path=denylist,
        private_denylist_sha256=denylist_sha256,
    )

    expected_hashes = sorted(
        hashlib.sha256(value.encode()).hexdigest() for value in (locator, phrase)
    )
    serialized = report_path.read_text(encoding="utf-8")
    assert report["status"] == "failed"
    assert report["private_literal_denylist_sha256"] == denylist_sha256
    assert report["matched_private_literal_sha256"] == expected_hashes
    assert any(
        finding.startswith("git_history_blob:")
        and ":private_literal:" in finding
        for finding in report["findings"]
    )
    assert any(
        finding.startswith("git_history_commit:")
        and ":private_literal:" in finding
        for finding in report["findings"]
    )
    assert locator not in serialized
    assert phrase not in serialized
    assert str(denylist) not in serialized


def test_private_denylist_scans_current_tree_archive_paths_and_artifacts(
    tmp_path: Path,
) -> None:
    repository, _sdist, _wheel = _release_repository(tmp_path)
    base = _run_git(repository, "rev-parse", "HEAD")
    locator = "privatefixturekey42"
    phrase = "private source-grounded judgment"
    module = repository / "src" / "auto_zettelkasten" / f"{locator}.py"
    module.write_text(f"# {phrase}\n", encoding="utf-8")
    _run_git(repository, "add", str(module.relative_to(repository)))
    _run_git(repository, "commit", "-qm", "add private fixture")
    sdist, wheel = _write_release_archives(repository, tmp_path / "private-build")
    artifact = tmp_path / "release" / f"{locator}.yml"
    artifact.parent.mkdir()
    artifact.write_text(phrase + "\n", encoding="utf-8")
    denylist, denylist_sha256 = _private_literal_denylist(
        tmp_path,
        [("locator", locator), ("judgment", phrase)],
    )

    report = audit_tool.package_audit(
        repository,
        sdist,
        wheel,
        tmp_path / "private-report" / "current.yml",
        base_ref=base,
        artifacts=(artifact,),
        private_denylist_path=denylist,
        private_denylist_sha256=denylist_sha256,
    )

    private_findings = [
        finding for finding in report["findings"] if ":private_literal:" in finding
    ]
    assert report["status"] == "failed"
    assert any(finding.startswith("git_tree:") for finding in private_findings)
    assert any(finding.startswith("git_tree_path:") for finding in private_findings)
    assert any(finding.startswith("sdist:") for finding in private_findings)
    assert any(finding.startswith("wheel:") for finding in private_findings)
    assert any(finding.startswith("artifact:") for finding in private_findings)
    assert any(finding.startswith("artifact_path:") for finding in private_findings)
    assert locator not in json.dumps(report)
    assert phrase not in json.dumps(report)


def test_private_denylist_requires_bound_canonical_unique_input(tmp_path: Path) -> None:
    repository, sdist, wheel = _release_repository(tmp_path)
    denylist, digest = _private_literal_denylist(
        tmp_path, [("one", "PRIVATEVALUE42")]
    )
    arguments = (repository, sdist, wheel, tmp_path / "private-report" / "audit.yml")

    with pytest.raises(ValueError, match="supplied together"):
        audit_tool.package_audit(*arguments, private_denylist_path=denylist)
    denylist.write_text(denylist.read_text() + " ", encoding="ascii")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        audit_tool.package_audit(
            *arguments,
            private_denylist_path=denylist,
            private_denylist_sha256=digest,
        )
    denylist, digest = _private_literal_denylist(
        tmp_path,
        [("one", "PRIVATEVALUE42"), ("two", "privatevalue42")],
    )
    with pytest.raises(ValueError, match="duplicate literals"):
        audit_tool.package_audit(
            *arguments,
            private_denylist_path=denylist,
            private_denylist_sha256=digest,
        )


def test_private_denylist_does_not_leak_unsafe_archive_member(tmp_path: Path) -> None:
    repository, sdist, wheel = _release_repository(tmp_path)
    literal = "PRIVATEVALUE42"
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr(f"../{literal}.py", "")
    denylist, digest = _private_literal_denylist(
        tmp_path, [("unsafe-member", literal)]
    )

    with pytest.raises(ValueError, match="unsafe or duplicate archive member") as error:
        audit_tool.package_audit(
            repository,
            sdist,
            wheel,
            tmp_path / "private-report" / "audit.yml",
            private_denylist_path=denylist,
            private_denylist_sha256=digest,
        )

    assert literal not in str(error.value)


def test_private_denylist_does_not_leak_corrupt_archive_member(tmp_path: Path) -> None:
    repository, sdist, _wheel = _release_repository(tmp_path)
    literal = "PRIVATEVALUE42"
    payload = b"UNIQUE-CORRUPT-PAYLOAD"
    _, wheel = _write_release_archives(
        repository,
        tmp_path / "corrupt-wheel",
        extra_wheel=(f"auto_zettelkasten/{literal}.py", payload),
    )
    damaged = bytearray(wheel.read_bytes())
    payload_offset = damaged.index(payload)
    damaged[payload_offset] ^= 1
    wheel.write_bytes(damaged)
    denylist, digest = _private_literal_denylist(
        tmp_path, [("corrupt-member", literal)]
    )

    with pytest.raises(ValueError, match="release archive is unreadable") as error:
        audit_tool.package_audit(
            repository,
            sdist,
            wheel,
            tmp_path / "private-report" / "audit.yml",
            private_denylist_path=denylist,
            private_denylist_sha256=digest,
        )

    assert literal not in str(error.value)


def test_package_audit_rejects_wheel_not_built_from_inspected_sdist(
    tmp_path: Path,
) -> None:
    repository, sdist, _wheel = _release_repository(tmp_path)
    _, wheel = _write_release_archives(
        repository,
        tmp_path / "mismatched-artifacts",
        wheel_source=b'__version__ = "different"\n',
    )

    report = audit_tool.package_audit(
        repository,
        sdist,
        wheel,
        tmp_path / "private" / "mismatch.yml",
        base_ref="HEAD",
    )

    assert report["status"] == "failed"
    assert "wheel:auto_zettelkasten/__init__.py:sdist_source_mismatch" in report[
        "findings"
    ]


@pytest.mark.parametrize(
    ("member", "content", "expected"),
    [
        (
            "auto_zettelkasten-0.30.0.dist-info/METADATA",
            b"Name: auto-zettelkasten\nVersion: 0.30.0\n"
            b"Requires-Dist: unexpected-package\n",
            "wheel:metadata_sdist_mismatch",
        ),
        (
            "auto_zettelkasten-0.30.0.dist-info/entry_points.txt",
            b"[console_scripts]\nauto-zettelkasten = other:main\n",
            "wheel:entry_points_mismatch",
        ),
        (
            "auto_zettelkasten-0.30.0.dist-info/WHEEL",
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: py2-none-any\n",
            "wheel:wheel_metadata",
        ),
        (
            "auto_zettelkasten-0.30.0.dist-info/licenses/LICENSE",
            b"different license\n",
            "wheel:license_sdist_mismatch",
        ),
    ],
)
def test_package_audit_rejects_modified_wheel_metadata(
    tmp_path: Path,
    member: str,
    content: bytes,
    expected: str,
) -> None:
    repository, sdist, _wheel = _release_repository(tmp_path)
    _, wheel = _write_release_archives(
        repository,
        tmp_path / "modified-metadata",
        wheel_overrides={member: content},
    )

    report = audit_tool.package_audit(
        repository,
        sdist,
        wheel,
        tmp_path / "private" / "modified-metadata.yml",
        base_ref="HEAD",
    )

    assert report["status"] == "failed"
    assert expected in report["findings"]


def test_package_audit_rejects_dependency_injected_into_both_metadata_files(
    tmp_path: Path,
) -> None:
    repository, _sdist, _wheel = _release_repository(tmp_path)
    injected = (
        b"Name: auto-zettelkasten\nVersion: 0.30.0\n"
        b"Requires-Dist: unexpected-package\n"
    )
    sdist, wheel = _write_release_archives(
        repository,
        tmp_path / "injected-dependency",
        package_info_override=injected,
    )

    report = audit_tool.package_audit(
        repository,
        sdist,
        wheel,
        tmp_path / "private" / "injected-dependency.yml",
        base_ref="HEAD",
    )

    assert report["status"] == "failed"
    assert "sdist:pkg_info_pyproject_mismatch" in report["findings"]
    assert "wheel:metadata_sdist_mismatch" not in report["findings"]


def test_package_audit_rejects_wheel_filename_tag_mismatch(tmp_path: Path) -> None:
    repository, sdist, wheel = _release_repository(tmp_path)
    renamed = wheel.with_name("auto_zettelkasten-0.30.0-py2-none-any.whl")
    wheel.rename(renamed)

    report = audit_tool.package_audit(
        repository,
        sdist,
        renamed,
        tmp_path / "private" / "filename-tag.yml",
        base_ref="HEAD",
    )

    assert report["status"] == "failed"
    assert "wheel:filename_tag" in report["findings"]


def test_package_audit_rejects_sdist_filename_mismatch(tmp_path: Path) -> None:
    repository, sdist, wheel = _release_repository(tmp_path)
    renamed = sdist.with_name("renamed-release.tar.gz")
    sdist.rename(renamed)

    report = audit_tool.package_audit(
        repository,
        renamed,
        wheel,
        tmp_path / "private" / "sdist-filename.yml",
        base_ref="HEAD",
    )

    assert report["status"] == "failed"
    assert "sdist:filename" in report["findings"]


def test_package_audit_rejects_wrong_record_hashes_and_sizes(tmp_path: Path) -> None:
    repository, sdist, _wheel = _release_repository(tmp_path)
    _, wheel = _write_release_archives(
        repository,
        tmp_path / "wrong-record",
        corrupt_record=True,
    )

    report = audit_tool.package_audit(
        repository,
        sdist,
        wheel,
        tmp_path / "private" / "wrong-record.yml",
        base_ref="HEAD",
    )

    assert report["status"] == "failed"
    assert "wheel:record_inventory" not in report["findings"]
    assert "wheel:record_hash_or_size" in report["findings"]


def test_package_audit_rejects_test_members_and_explicit_directories(
    tmp_path: Path,
) -> None:
    repository, sdist, _wheel = _release_repository(tmp_path)
    _, wheel = _write_release_archives(
        repository,
        tmp_path / "test-member",
        extra_wheel=("tests/test_private.py", b"pass\n"),
    )
    report = audit_tool.package_audit(
        repository,
        sdist,
        wheel,
        tmp_path / "private" / "test-member.yml",
        base_ref="HEAD",
    )
    assert "wheel:member_allowlist" in report["findings"]

    _, directory_wheel = _write_release_archives(
        repository,
        tmp_path / "explicit-directory",
        extra_wheel=("tests/", b""),
    )
    with pytest.raises(ValueError, match="explicit archive directory"):
        audit_tool.package_audit(
            repository,
            sdist,
            directory_wheel,
            tmp_path / "private" / "explicit-directory.yml",
            base_ref="HEAD",
        )


def test_package_audit_rejects_zip_symlink_masquerading_as_wheel_metadata(
    tmp_path: Path,
) -> None:
    repository, sdist, _wheel = _release_repository(tmp_path)
    wheel_member = "auto_zettelkasten-0.30.0.dist-info/WHEEL"
    _, wheel = _write_release_archives(
        repository,
        tmp_path / "symlink-member",
        symlink_member=wheel_member,
    )

    with pytest.raises(ValueError, match="non-regular archive member"):
        audit_tool.package_audit(
            repository,
            sdist,
            wheel,
            tmp_path / "private" / "symlink.yml",
            base_ref="HEAD",
        )


@pytest.mark.parametrize(
    ("member", "content", "expected"),
    [
        ("auto_zettelkasten/auth.json", b"{}\n", "credential_filename"),
        ("auto_zettelkasten/.netrc", b"machine example.invalid\n", "credential_filename"),
        (
            "auto_zettelkasten/github-token.txt",
            (_fake_github_pat() + "\n").encode(),
            "github_fine_grained_token",
        ),
        (
            "auto_zettelkasten/pypi-token.txt",
            (_fake_pypi_token() + "\n").encode(),
            "pypi_token",
        ),
        (
            "auto_zettelkasten/escaped-paths.json",
            _fake_json_private_roots(),
            "private_root",
        ),
        (
            "auto_zettelkasten/leak.py",
            (_fake_private_root() + "\n" + _fake_openai_token() + "\n").encode(),
            "private_root",
        ),
    ],
)
def test_package_audit_rejects_archive_allowlist_credentials_and_secrets(
    tmp_path: Path,
    member: str,
    content: bytes,
    expected: str,
) -> None:
    repository, sdist, _wheel = _release_repository(tmp_path)
    _, wheel = _write_release_archives(
        repository,
        tmp_path / "unsafe-artifacts",
        extra_wheel=(member, content),
    )

    report = audit_tool.package_audit(
        repository,
        sdist,
        wheel,
        tmp_path / "private" / "unsafe.yml",
        base_ref="HEAD",
    )

    assert report["status"] == "failed"
    assert "wheel:member_allowlist" in report["findings"]
    assert any(expected in finding for finding in report["findings"])
    if member.endswith("leak.py"):
        assert any("openai_token" in finding for finding in report["findings"])


def test_package_audit_rejects_archive_path_traversal(tmp_path: Path) -> None:
    repository, sdist, wheel = _release_repository(tmp_path)
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("../escape.py", b"pass\n")

    with pytest.raises(ValueError, match="unsafe or duplicate archive member"):
        audit_tool.package_audit(
            repository,
            sdist,
            wheel,
            tmp_path / "private" / "traversal.yml",
            base_ref="HEAD",
        )


def test_package_audit_scans_archive_container_metadata(tmp_path: Path) -> None:
    repository, sdist, wheel = _release_repository(tmp_path)
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.comment = (_fake_openai_token() + " " + _fake_private_root()).encode()

    report = audit_tool.package_audit(
        repository,
        sdist,
        wheel,
        tmp_path / "private" / "container-metadata.yml",
        base_ref="HEAD",
    )

    assert report["status"] == "failed"
    assert any(
        finding.startswith("wheel_container:")
        and finding.endswith(":openai_token")
        for finding in report["findings"]
    )
    assert any(
        finding.startswith("wheel_container:")
        and finding.endswith(":private_root")
        for finding in report["findings"]
    )


@pytest.mark.parametrize("wheel", [True, False])
def test_archive_declared_size_ceiling_is_checked_before_member_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wheel: bool,
) -> None:
    archive_path = tmp_path / ("oversize.whl" if wheel else "oversize.tar.gz")
    if wheel:
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("payload", b"123456789")
        monkeypatch.setattr(
            zipfile.ZipFile,
            "read",
            lambda *_args, **_kwargs: pytest.fail("member bytes must not be read"),
        )
    else:
        with tarfile.open(archive_path, "w:gz") as archive:
            info = tarfile.TarInfo("payload")
            info.size = 9
            archive.addfile(info, io.BytesIO(b"123456789"))
        monkeypatch.setattr(
            tarfile.TarFile,
            "extractfile",
            lambda *_args, **_kwargs: pytest.fail("member bytes must not be read"),
        )
    monkeypatch.setattr(audit_tool, "_MAX_ARCHIVE_BYTES", 8)

    with pytest.raises(ValueError, match="inspection byte ceiling"):
        audit_tool._archive_files(archive_path, wheel=wheel)


@pytest.mark.parametrize("wheel", [True, False])
def test_archive_container_size_ceiling_is_checked_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wheel: bool,
) -> None:
    archive_path = tmp_path / ("empty.whl" if wheel else "empty.tar.gz")
    if wheel:
        with zipfile.ZipFile(archive_path, "w"):
            pass
        monkeypatch.setattr(
            zipfile,
            "ZipFile",
            lambda *_args, **_kwargs: pytest.fail("archive must not be opened"),
        )
    else:
        with tarfile.open(archive_path, "w:gz"):
            pass
        monkeypatch.setattr(
            tarfile,
            "open",
            lambda *_args, **_kwargs: pytest.fail("archive must not be opened"),
        )
    monkeypatch.setattr(audit_tool, "_MAX_ARCHIVE_BYTES", archive_path.stat().st_size - 1)

    with pytest.raises(ValueError, match="inspection byte ceiling"):
        audit_tool._archive_files(archive_path, wheel=wheel)


def test_package_audit_scans_dirty_staged_and_base_range_inputs(
    tmp_path: Path,
) -> None:
    repository, sdist, wheel = _release_repository(tmp_path)
    base = _run_git(repository, "rev-parse", "HEAD")

    range_path = repository / "range.txt"
    range_path.write_text(_fake_private_root() + "\n", encoding="utf-8")
    _run_git(repository, "add", range_path.name)
    _run_git(repository, "commit", "-qm", "range fixture")

    staged_path = repository / "staged.txt"
    staged_path.write_text(_fake_openai_token() + "\n", encoding="utf-8")
    _run_git(repository, "add", staged_path.name)

    bearer = "Bearer " + "B" * 24
    readme = repository / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") + bearer + "\n")

    report = audit_tool.package_audit(
        repository,
        sdist,
        wheel,
        tmp_path / "private" / "git-inputs.yml",
        base_ref=base,
    )

    assert report["status"] == "failed"
    assert any(finding.startswith("git_diff:") for finding in report["findings"])
    assert any(finding.startswith("staged_diff:") for finding in report["findings"])
    assert any(
        finding.startswith("git_tree:README.md:") and "bearer_token" in finding
        for finding in report["findings"]
    )


def test_package_audit_rejects_safe_dirty_candidate_built_from_worktree(
    tmp_path: Path,
) -> None:
    repository, _sdist, _wheel = _release_repository(tmp_path)
    readme = repository / "README.md"
    readme.write_text("# Safe but uncommitted change\n", encoding="utf-8")
    sdist, wheel = _write_release_archives(repository, tmp_path / "dirty-build")

    report = audit_tool.package_audit(
        repository,
        sdist,
        wheel,
        tmp_path / "private" / "dirty-build.yml",
        base_ref="HEAD",
    )

    assert report["status"] == "failed"
    assert report["repository_dirty"] is True
    assert "git_tree:dirty_candidate_state" in report["findings"]
    assert "sdist:README.md:committed_head_mismatch" in report["findings"]


def test_package_audit_rejects_ignored_module_packaged_outside_head(
    tmp_path: Path,
) -> None:
    repository, _sdist, _wheel = _release_repository(tmp_path)
    exclude = repository / ".git" / "info" / "exclude"
    exclude.write_text("src/auto_zettelkasten/stealth.py\n", encoding="utf-8")
    stealth = repository / "src" / "auto_zettelkasten" / "stealth.py"
    stealth.write_text("VALUE = 1\n", encoding="utf-8")
    assert _run_git(repository, "status", "--porcelain") == ""
    sdist, wheel = _write_release_archives(repository, tmp_path / "stealth-build")

    report = audit_tool.package_audit(
        repository,
        sdist,
        wheel,
        tmp_path / "private" / "stealth-build.yml",
        base_ref="HEAD",
    )

    assert report["status"] == "failed"
    assert report["repository_dirty"] is False
    assert "sdist:member_allowlist" in report["findings"]
    assert "wheel:member_allowlist" in report["findings"]


def test_package_audit_scans_add_delete_history_and_commit_messages(
    tmp_path: Path,
) -> None:
    repository, sdist, wheel = _release_repository(tmp_path)
    base = _run_git(repository, "rev-parse", "HEAD")
    historical = repository / "credentials-old.json"
    historical.write_text(_fake_openai_token() + "\n", encoding="utf-8")
    _run_git(repository, "add", historical.name)
    _run_git(repository, "commit", "-qm", "add historical fixture")
    historical.unlink()
    _run_git(repository, "add", "-u")
    _run_git(repository, "commit", "-qm", "remove historical fixture")
    _run_git(
        repository,
        "commit",
        "--allow-empty",
        "-qm",
        "message " + _fake_openai_token(),
    )

    report = audit_tool.package_audit(
        repository,
        sdist,
        wheel,
        tmp_path / "private" / "history.yml",
        base_ref=base,
    )

    assert report["status"] == "failed"
    assert report["repository_dirty"] is False
    assert any(
        finding.startswith("git_history_blob:")
        and finding.endswith(":openai_token")
        for finding in report["findings"]
    )
    assert any(
        finding.startswith("git_history_commit:")
        and finding.endswith(":openai_token")
        for finding in report["findings"]
    )
    assert (
        "git_history_path:credentials-old.json:credential_filename"
        in report["findings"]
    )


@pytest.mark.parametrize(
    "unsafe_name", [_fake_openai_token(), _private_workspace_marker()]
)
def test_package_audit_scans_current_and_historical_filenames(
    tmp_path: Path,
    unsafe_name: str,
) -> None:
    repository, sdist, wheel = _release_repository(tmp_path)
    base = _run_git(repository, "rev-parse", "HEAD")
    path = repository / "docs" / unsafe_name
    path.parent.mkdir()
    path.write_text("benign contents\n", encoding="utf-8")
    _run_git(repository, "add", str(path.relative_to(repository)))
    _run_git(repository, "commit", "-qm", "add filename scan fixture")

    report = audit_tool.package_audit(
        repository,
        sdist,
        wheel,
        tmp_path / "private" / "filename-scan.yml",
        base_ref=base,
    )

    assert report["status"] == "failed"
    assert any(
        finding.startswith("git_tree_path:path-")
        and ":filename_" in finding
        for finding in report["findings"]
    )
    assert any(
        finding.startswith("git_history_path:path-")
        and ":filename_" in finding
        for finding in report["findings"]
    )
    assert all(unsafe_name not in finding for finding in report["findings"])


@pytest.mark.parametrize("suffix", ["", "REALSECRETSUFFIX123456789"])
def test_history_sentinel_exemption_requires_an_exact_token_match(
    tmp_path: Path,
    suffix: str,
) -> None:
    repository, sdist, wheel = _release_repository(tmp_path)
    base = _run_git(repository, "rev-parse", "HEAD")
    sentinel = "s" + "k-" + "SYNTHETICINVALID0000" + suffix
    historical = repository / "tests" / "test_codex_provider.py"
    historical.parent.mkdir()
    historical.write_text(sentinel + "\n", encoding="utf-8")
    _run_git(repository, "add", str(historical.relative_to(repository)))
    _run_git(repository, "commit", "-qm", "add historical test sentinel")
    historical.unlink()
    _run_git(repository, "add", "-u")
    _run_git(repository, "commit", "-qm", "remove historical test sentinel")

    report = audit_tool.package_audit(
        repository,
        sdist,
        wheel,
        tmp_path / "private" / f"sentinel-{bool(suffix)}.yml",
        base_ref=base,
    )

    if suffix:
        assert report["status"] == "failed"
        assert any(
            finding.startswith("git_history_blob:")
            and finding.endswith(":openai_token")
            for finding in report["findings"]
        )
        assert report["history_test_sentinel_policy"]["exemptions"] == []
    else:
        assert report["status"] == "passed"
        assert report["history_test_sentinel_policy"]["exemptions"]


@pytest.mark.parametrize(
    ("relative", "suffix", "keep_current", "accepted"),
    [
        ("tests/test_codex_provider.py", "", False, True),
        ("tests/test_codex_provider.py", ".backup", False, False),
        ("tests/test_other.py", "", False, False),
        ("tests/test_codex_provider.py", "", True, False),
    ],
)
def test_historical_private_path_exception_is_exact_and_history_only(
    tmp_path: Path, relative: str, suffix: str, keep_current: bool, accepted: bool
) -> None:
    repository, sdist, wheel = _release_repository(tmp_path)
    base = _run_git(repository, "rev-parse", "HEAD")
    path = repository / relative
    path.parent.mkdir()
    sentinel = "/" + "Users" + "/private/.codex/auth.json" + suffix
    path.write_text(f'    private_path = "{sentinel}"\n', encoding="utf-8")
    _run_git(repository, "add", relative)
    _run_git(repository, "commit", "-qm", "historical synthetic path")
    if not keep_current:
        path.unlink()
        _run_git(repository, "add", "-u")
        _run_git(repository, "commit", "-qm", "remove synthetic path")

    report = audit_tool.package_audit(
        repository, sdist, wheel, tmp_path / "private" / "path-sentinel.yml",
        base_ref=base,
    )

    assert (report["status"] == "passed") is accepted
    if accepted:
        assert report["history_test_sentinel_policy"]["exemptions"] == [
            {
                "object_id": _run_git(repository, "rev-parse", f"HEAD~1:{relative}"),
                "sentinel_sha256": hashlib.sha256(sentinel.encode()).hexdigest(),
                "occurrences": 1,
            }
        ]
    else:
        assert any(finding.endswith(":private_root") for finding in report["findings"])


def test_package_audit_rejects_tracked_codex_credentials_directory(
    tmp_path: Path,
) -> None:
    repository, sdist, wheel = _release_repository(tmp_path)
    base = _run_git(repository, "rev-parse", "HEAD")
    config = repository / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text("model = \"example\"\n", encoding="utf-8")
    _run_git(repository, "add", "-f", str(config.relative_to(repository)))
    _run_git(repository, "commit", "-qm", "tracked credential directory")

    report = audit_tool.package_audit(
        repository,
        sdist,
        wheel,
        tmp_path / "private" / "codex-directory.yml",
        base_ref=base,
    )

    assert report["status"] == "failed"
    assert "git_tree:.codex/config.toml:credential_filename" in report["findings"]
    assert (
        "git_history_path:.codex/config.toml:credential_filename"
        in report["findings"]
    )
