from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_zettelkasten.extraction import (
    ContentAdequacy,
    ContentAdequacyClass,
    ExtractionResult,
    PDFPageEvidence,
    PDFPageImage,
    PDFStructuralProbe,
)
from auto_zettelkasten.models import MapRequest
from auto_zettelkasten.pipeline import (
    _custodied_pdf_candidate,
    _prepare_item,
    _read_document,
    _recover_pdf_image_route,
    _render_document_route_attachments,
)
from auto_zettelkasten.readers import (
    CloudPermissionError,
    CodexReader,
    ProviderInvalidSourceBundle,
    ProviderIsolationFailure,
    ProviderQuotaExhausted,
    ProviderUnsupportedAttachment,
    _SOURCE_BUNDLE_ATTACHMENTS,
    _codex_failure,
    codex_source_bundle_attachment_identity,
    codex_source_bundle_image_preflight,
)
from auto_zettelkasten.relationships import stable_hash
from auto_zettelkasten.files import read_yaml, write_yaml
from conftest import fake_codex_preflight


def _route(custody: Path, *, rendered_images: list[dict] | None = None) -> dict:
    identity_payload = {
        "route_version": "1",
        "route": "codex_pdf_page_images",
        "custody_file": str(custody.resolve()),
        "custody_sha256": hashlib.sha256(custody.read_bytes()).hexdigest(),
        "selected_pages": [1],
        "render_policy": {
            "format": "png",
            "maximum_side": 2_048,
            "maximum_pages": 16,
            "enlargement": False,
        },
        "attachment_capability": codex_source_bundle_attachment_identity(),
        "probe_evidence": {},
        "projected_preflight": {"admitted": True},
    }
    return {
        "identity_payload": identity_payload,
        "identity": stable_hash(identity_payload),
        "rendered_images": rendered_images or [],
        "recovery": {"state": "not_selected"},
    }


def _bundle() -> dict:
    return {
        "bundle_schema_version": "1",
        "source_identity": {"source_id": "source-zotero-A1", "zotero_key": "A1"},
        "observed_bibliographic_identity": {},
        "scope_assessment": {},
        "analysis_sections": {"thesis": "Grounded result."},
        "compact_profile": {},
        "evidence_anchors": [],
        "literature_positions": [],
        "missing_source_recommendations": [],
        "self_review": {"passed": True},
    }


def _request(workspace: Path) -> MapRequest:
    return MapRequest(
        workspace,
        provider="codex",
        model="gpt-5.6-luna",
        literature_model="gpt-5.6-terra",
        allow_cloud=True,
    )


def test_image_preflight_uses_pinned_formula_and_inclusive_ceiling(monkeypatch) -> None:
    from auto_zettelkasten import readers

    monkeypatch.setattr(readers, "_codex_wire_prompt", lambda *_args: "1000")
    monkeypatch.setattr(readers, "_estimate_tokens", lambda value: int(value))

    small = codex_source_bundle_image_preflight("", {}, None, [(33, 65)])
    assert small["image_tokens"] == 8  # ceil(ceil(33/32) * ceil(65/32) * 1.2)

    exact = codex_source_bundle_image_preflight("", {}, None, [(64, 1_561_056)])
    over = codex_source_bundle_image_preflight("", {}, None, [(64, 1_561_088)])
    assert exact["combined_tokens"] == 200_000 and exact["admitted"] is True
    assert over["combined_tokens"] > 200_000 and over["admitted"] is False


def test_codex_command_is_unchanged_without_images_and_orders_image_args(
    tmp_path: Path,
) -> None:
    capture = tmp_path / "calls.jsonl"
    executable = tmp_path / "codex"
    executable.write_text(
        f"""#!{sys.executable}
import json, sys
from pathlib import Path
with Path({str(capture)!r}).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(sys.argv) + "\\n")
print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": "{{}}"}}}}), flush=True)
print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 1, "output_tokens": 1}}}}), flush=True)
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    first.write_bytes(b"\x89PNG\r\n\x1a\nfirst")
    second.write_bytes(b"\x89PNG\r\n\x1a\nsecond")
    reader = CodexReader("gpt-5.6-luna", allow_cloud=True)
    reader._preflight = fake_codex_preflight(tmp_path, executable, {})

    reader._generate_with_reasoning(
        "system", "user", 2_048, 5, reasoning_effort="medium", output_contract="source_bundle"
    )
    token = _SOURCE_BUNDLE_ATTACHMENTS.set((first, second))
    try:
        reader._generate_with_reasoning(
            "system", "user", 2_048, 5, reasoning_effort="medium", output_contract="source_bundle"
        )
    finally:
        _SOURCE_BUNDLE_ATTACHMENTS.reset(token)

    plain, imaged = [json.loads(line) for line in capture.read_text().splitlines()]
    assert plain[1:3] == ["exec", "-"] and "--image" not in plain
    assert imaged[1:6] == ["exec", "--image", str(first), "--image", str(second)]
    assert imaged[6] == "-"


def test_unsupported_attachment_is_typed_and_private_paths_are_redacted(
    tmp_path: Path,
) -> None:
    attachment = tmp_path / "private" / "page.png"
    failure = _codex_failure(
        f"image inputs are not supported: {attachment}",
        tmp_path / "credentials",
        (attachment,),
    )
    assert isinstance(failure, ProviderUnsupportedAttachment)
    assert str(attachment) not in str(failure)
    assert "[REDACTED_ATTACHMENT]" in str(failure)


def test_custodied_auto_pdf_selects_image_route_without_local_ocr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from auto_zettelkasten import pipeline

    adequacy = ContentAdequacy(
        ContentAdequacyClass.PARTIAL_PDF_TEXT,
        "fulltext_available",
        "limited",
        "image_only",
        metrics={"page_count": 1},
    )
    page = PDFPageEvidence(
        1, "1", 612, 792, "", hashlib.sha256(b"").hexdigest(), 0, 0,
        "image_only", ("/Image",), 1, 1, 1, True, True, True,
    )
    probe = PDFStructuralProbe(
        "succeeded", "", "application/pdf", hashlib.sha256(b"pdf").hexdigest(),
        3, 1, (page,), "", adequacy, {}, (1,), (1,), ("1",),
    )
    monkeypatch.setattr(pipeline, "probe_pdf_bytes", lambda *_args, **_kwargs: probe)
    monkeypatch.setattr(
        pipeline,
        "extract_pdf_from_probe",
        lambda *_args, **_kwargs: pytest.fail("local OCR must not run before routing"),
    )
    custody = tmp_path / "custody.pdf"
    custody.write_bytes(b"pdf")
    item = {"key": "A1", "data": {"key": "A1", "itemType": "journalArticle"}}
    candidate, extracted = _custodied_pdf_candidate(
        b"pdf", custody, {}, item, {"source_id": "source-zotero-A1"},
        _request(tmp_path),
        actual_primary_pdf=True, cancelled=None,
    )
    assert extracted.route == "codex_pdf_page_images"
    assert candidate and candidate["document_route"]["identity_payload"]["selected_pages"] == [1]


def test_full_text_codex_pdf_images_only_collapsed_percent_charts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from auto_zettelkasten import pipeline

    prose = "Substantive embedded prose remains available for source analysis. " * 10
    pages = tuple(
        PDFPageEvidence(
            number,
            str(number),
            612,
            792,
            "1%2%3%4% Chart labels" if number == 17 else prose,
            hashlib.sha256(prose.encode()).hexdigest(),
            len(prose),
            70,
            "good",
            ("/Image",),
            1,
            1,
            1,
            False,
            False,
            False,
        )
        for number in range(1, 18)
    )
    adequacy = ContentAdequacy(
        ContentAdequacyClass.FULL_PDF_TEXT,
        "full_document",
        "passed",
        "full_pdf_text",
        metrics={"page_count": 17},
    )
    probe = PDFStructuralProbe(
        "succeeded",
        "",
        "application/pdf",
        hashlib.sha256(b"pdf").hexdigest(),
        3,
        17,
        pages,
        prose,
        adequacy,
        {},
        (),
        tuple(range(1, 18)),
        tuple(str(number) for number in range(1, 18)),
    )
    monkeypatch.setattr(pipeline, "probe_pdf_bytes", lambda *_args, **_kwargs: probe)
    monkeypatch.setattr(
        pipeline,
        "extract_pdf_from_probe",
        lambda *_args, **_kwargs: pytest.fail("local OCR must not run before routing"),
    )
    custody = tmp_path / "custody.pdf"
    custody.write_bytes(b"pdf")
    item = {"key": "A1", "data": {"key": "A1", "itemType": "journalArticle"}}

    candidate, extracted = _custodied_pdf_candidate(
        b"pdf",
        custody,
        {},
        item,
        {"source_id": "source-zotero-A1"},
        _request(tmp_path),
        actual_primary_pdf=True,
        cancelled=None,
    )

    assert extracted.route == "codex_pdf_page_images"
    assert candidate and candidate["document_route"]["identity_payload"][
        "selected_pages"
    ] == [17]


def test_non_codex_pdf_keeps_existing_extraction_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from auto_zettelkasten import pipeline

    text = "evidence " * 300
    adequacy = ContentAdequacy(
        ContentAdequacyClass.FULL_PDF_TEXT,
        "full_document",
        "passed",
        "full_pdf_text",
        metrics={"page_count": 1},
    )
    page = PDFPageEvidence(
        1, "1", 612, 792, text, hashlib.sha256(text.encode()).hexdigest(),
        len(text), 300, "good", ("Font",), 1, 0, 0, False, False, False,
    )
    probe = PDFStructuralProbe(
        "succeeded", "", "application/pdf", hashlib.sha256(b"pdf").hexdigest(),
        3, 1, (page,), text, adequacy, {}, (), (), ("1",),
    )
    calls = 0

    def extract(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return ExtractionResult(
            status="succeeded",
            text=text,
            route="pypdf_text",
            media_type="application/pdf",
            page_count=1,
            adequacy=adequacy,
        )

    monkeypatch.setattr(pipeline, "probe_pdf_bytes", lambda *_args, **_kwargs: probe)
    monkeypatch.setattr(pipeline, "extract_pdf_from_probe", extract)
    custody = tmp_path / "custody.pdf"
    custody.write_bytes(b"pdf")
    item = {"key": "A1", "data": {"key": "A1", "itemType": "journalArticle"}}
    candidate, _ = _custodied_pdf_candidate(
        b"pdf", custody, {}, item, {"source_id": "source-zotero-A1"},
        MapRequest(tmp_path, provider="ollama", model="fake"),
        actual_primary_pdf=True, cancelled=None,
    )
    assert calls == 1
    assert candidate and "document_route" not in candidate


def test_completed_direct_checkpoint_bypasses_rerender(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from auto_zettelkasten import pipeline

    custody = tmp_path / "custody.pdf"
    custody.write_bytes(b"pdf")
    route = _route(custody)
    image = tmp_path / "page.png"
    image.write_bytes(b"png")
    rendered = PDFPageImage(1, image, "image/png", 100, 200, "abc", 3, "fake", "1", "1")
    monkeypatch.setattr(pipeline, "render_pdf_pages", lambda *_args, **_kwargs: [rendered])

    class Reader:
        name = "codex"
        model = "gpt-5.6-luna"
        context_window_tokens = 272_000

        def read_source_bundle(self, *_args, **_kwargs):
            return _bundle()

    request = _request(tmp_path)
    checkpoint = tmp_path / "checkpoint"
    first = _read_document(
        Reader(), "", {"_source_context": {"source_id": "source-zotero-A1", "zotero_key": "A1"}},
        None, request=request, checkpoint_root=checkpoint, document_route=route,
        expected_custody_hash=hashlib.sha256(b"pdf").hexdigest(),
        expected_custody_file=custody,
        custody_root=tmp_path,
    )
    monkeypatch.setattr(pipeline, "render_pdf_pages", lambda *_args, **_kwargs: pytest.fail("rerendered"))
    second = _read_document(
        Reader(), "", {"_source_context": {"source_id": "source-zotero-A1", "zotero_key": "A1"}},
        None, request=request, checkpoint_root=checkpoint, document_route=route,
        expected_custody_hash=hashlib.sha256(b"pdf").hexdigest(),
        expected_custody_file=custody,
        custody_root=tmp_path,
    )
    assert first[0] == second[0]
    assert second[2] == "reused_direct_source_checkpoint"


def test_incomplete_route_rerenders_and_rejects_hash_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from auto_zettelkasten import pipeline

    custody = tmp_path / "custody.pdf"
    custody.write_bytes(b"pdf")
    expected = [{"page_number": 1, "sha256": "old"}]
    route = _route(custody, rendered_images=expected)
    image = tmp_path / "page.png"
    image.write_bytes(b"png")
    rendered = PDFPageImage(1, image, "image/png", 100, 200, "new", 3, "fake", "1", "1")
    calls = 0

    def render(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return [rendered]

    monkeypatch.setattr(pipeline, "render_pdf_pages", render)
    with pytest.raises(ProviderIsolationFailure, match="rerender hash mismatch"):
        _render_document_route_attachments(
            route, text="", metadata={}, question=None, checkpoint_root=tmp_path / "checkpoint",
            output_dir=tmp_path / "images", cancelled=None,
            expected_custody_hash=hashlib.sha256(b"pdf").hexdigest(),
            expected_custody_file=custody,
            custody_root=tmp_path,
        )
    assert calls == 1


def test_route_custody_is_bound_to_frozen_content_and_workspace(
    tmp_path: Path,
) -> None:
    custody_root = tmp_path / "workspace" / "01_custody" / "files"
    custody_root.mkdir(parents=True)
    expected = custody_root / "expected.pdf"
    expected.write_bytes(b"expected")
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"outside")
    route = _route(outside)

    with pytest.raises(ProviderIsolationFailure, match="custody binding"):
        _read_document(
            SimpleNamespace(
                name="codex",
                model="gpt-5.6-luna",
                context_window_tokens=272_000,
            ),
            "",
            {},
            None,
            request=_request(tmp_path),
            checkpoint_root=tmp_path / "checkpoint",
            document_route=route,
            expected_custody_hash=hashlib.sha256(b"expected").hexdigest(),
            expected_custody_file=expected,
            custody_root=custody_root,
        )


@pytest.mark.parametrize(
    ("status", "text"),
    [("failed", ""), ("succeeded", "   ")],
)
def test_failed_local_recovery_is_terminal_on_resume(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: str, text: str
) -> None:
    from auto_zettelkasten import pipeline

    workspace = tmp_path / "workspace"
    custody_root = workspace / "01_custody" / "files"
    custody_root.mkdir(parents=True)
    custody = custody_root / "source.pdf"
    custody.write_bytes(b"pdf")
    checkpoint = tmp_path / "run" / "items" / "A1"
    route = _route(custody)
    content = {
        "text": "",
        "content_hash": hashlib.sha256(b"pdf").hexdigest(),
        "source_file": str(custody),
        "content_route": "codex_pdf_page_images",
        "media_type": "application/pdf",
        "source_scope": "full_document",
        "source_coverage": {},
        "coverage_reason": "image_only",
        "coverage_metrics": {},
        "document_route": route,
    }
    monkeypatch.setattr(
        pipeline,
        "extract_path",
        lambda *_args, **_kwargs: ExtractionResult(
            status=status,
            text=text,
            route="pypdf_text",
            reason="ocr_unavailable",
            media_type="application/pdf",
        ),
    )
    with pytest.raises(ProviderInvalidSourceBundle, match="local recovery failed"):
        _recover_pdf_image_route(
            content,
            _request(workspace),
            workspace,
            checkpoint,
            trigger="ProviderUnsupportedAttachment",
            acquisition_gate=None,
            cancel_event=None,
        )
    failed_route = read_yaml(checkpoint / "document_route.yml", {})
    assert failed_route["recovery"]["state"] == "failed"

    content["document_route"] = failed_route
    monkeypatch.setattr(pipeline, "_load_frozen_content", lambda *_args: dict(content))
    monkeypatch.setattr(
        pipeline,
        "_recover_pdf_image_route",
        lambda *_args, **_kwargs: pytest.fail("terminal recovery retried"),
    )
    reader = SimpleNamespace(name="codex", model="gpt-5.6-luna", is_cloud=True)
    item = {"key": "A1", "data": {"key": "A1", "itemType": "journalArticle"}}
    result = _prepare_item(
        workspace,
        tmp_path / "run",
        0,
        item,
        _request(workspace),
        SimpleNamespace(),
        reader,
        None,
    )
    assert result["reason"] == "pdf_image_local_recovery_failed"


def test_local_recovery_preserves_rendered_route_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from auto_zettelkasten import pipeline

    workspace = tmp_path / "workspace"
    custody_root = workspace / "01_custody" / "files"
    custody_root.mkdir(parents=True)
    custody = custody_root / "source.pdf"
    custody.write_bytes(b"pdf")
    checkpoint = tmp_path / "run" / "items" / "A1"
    checkpoint.mkdir(parents=True)
    route = _route(custody)
    rendered_images = [{"page_number": 1, "sha256": "a" * 64}]
    actual_preflight = {"combined_tokens": 80_000, "admitted": True}
    persisted_route = {
        **route,
        "rendered_images": rendered_images,
        "actual_preflight": actual_preflight,
    }
    write_yaml(checkpoint / "document_route.yml", persisted_route)
    content = {
        "text": "",
        "content_hash": hashlib.sha256(b"pdf").hexdigest(),
        "source_file": str(custody),
        "content_route": "codex_pdf_page_images",
        "media_type": "application/pdf",
        "source_scope": "full_document",
        "source_coverage": {},
        "coverage_reason": "image_only",
        "coverage_metrics": {},
        "document_route": route,
    }
    monkeypatch.setattr(
        pipeline,
        "extract_path",
        lambda *_args, **_kwargs: ExtractionResult(
            status="succeeded",
            text="Recovered local text.",
            route="tesseract_ocr",
            media_type="application/pdf",
            page_count=1,
        ),
    )

    recovered = _recover_pdf_image_route(
        content,
        _request(workspace),
        workspace,
        checkpoint,
        trigger="ProviderUnsupportedAttachment",
        acquisition_gate=None,
        cancel_event=None,
    )

    saved_route = read_yaml(checkpoint / "document_route.yml", {})
    assert recovered["document_route"]["rendered_images"] == rendered_images
    assert saved_route["actual_preflight"] == actual_preflight
    assert saved_route["recovery"]["state"] == "completed"


def test_local_recovery_preserves_metadata_only_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from auto_zettelkasten import pipeline

    workspace = tmp_path / "workspace"
    custody_root = workspace / "01_custody" / "files"
    custody_root.mkdir(parents=True)
    custody = custody_root / "source.pdf"
    custody.write_bytes(b"pdf")
    checkpoint = tmp_path / "run" / "items" / "A1"
    content = {
        "text": "",
        "content_hash": hashlib.sha256(b"pdf").hexdigest(),
        "source_file": str(custody),
        "content_route": "codex_pdf_page_images",
        "media_type": "application/pdf",
        "source_scope": "partial_document",
        "source_coverage": {"source_scope": "partial_document"},
        "coverage_reason": "bounded_attachment_excerpt",
        "coverage_metrics": {},
        "document_route": _route(custody),
    }
    monkeypatch.setattr(
        pipeline,
        "extract_path",
        lambda *_args, **_kwargs: ExtractionResult(
            status="succeeded",
            text="Cover title",
            route="pypdf_text",
            media_type="application/pdf",
            page_count=1,
            adequacy=ContentAdequacy(
                classification=ContentAdequacyClass.METADATA_ONLY,
                source_scope="metadata_only",
                coverage_gate="failed",
                reason="insufficient_pdf_text_density",
            ),
        ),
    )

    recovered = _recover_pdf_image_route(
        content,
        _request(workspace),
        workspace,
        checkpoint,
        trigger="ProviderUnsupportedAttachment",
        acquisition_gate=None,
        cancel_event=None,
    )

    assert recovered["source_scope"] == "metadata_only"
    assert recovered["coverage_reason"] == "insufficient_pdf_text_density"


def test_metadata_only_immediate_recovery_stops_before_second_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from auto_zettelkasten import pipeline

    content = {
        "text": "",
        "content_hash": "hash",
        "source_file": str(tmp_path / "custody.pdf"),
        "content_route": "codex_pdf_page_images",
        "media_type": "application/pdf",
        "source_scope": "full_document",
        "source_coverage": {},
        "coverage_reason": "image_only",
        "coverage_metrics": {},
        "document_route": {
            "recovery": {"state": "not_selected"},
            "identity": "id",
        },
    }
    monkeypatch.setattr(pipeline, "_load_frozen_content", lambda *_args: dict(content))
    monkeypatch.setattr(
        pipeline, "_compatible_committed_note", lambda *_args, **_kwargs: None
    )
    reads = 0

    def read(*_args, **_kwargs):
        nonlocal reads
        reads += 1
        raise ProviderUnsupportedAttachment("recoverable")

    def recover(value, *_args, **_kwargs):
        return {
            **value,
            "text": "Cover title",
            "content_route": "pypdf_text_after_codex_image_recovery",
            "source_scope": "metadata_only",
            "coverage_reason": "insufficient_pdf_text_density",
        }

    monkeypatch.setattr(pipeline, "_read_document", read)
    monkeypatch.setattr(pipeline, "_recover_pdf_image_route", recover)
    reader = SimpleNamespace(name="codex", model="gpt-5.6-luna", is_cloud=True)
    item = {
        "key": "A1",
        "data": {"key": "A1", "itemType": "journalArticle", "title": "A"},
    }

    result = _prepare_item(
        tmp_path,
        tmp_path / "run",
        0,
        item,
        _request(tmp_path),
        SimpleNamespace(),
        reader,
        None,
    )

    assert result["terminal_status"] == "limited_note"
    assert result["note_status"] == "metadata_only_source_note"
    assert reads == 1


@pytest.mark.parametrize("failure_type", [ProviderUnsupportedAttachment, ProviderInvalidSourceBundle])
def test_only_typed_attachment_failures_select_one_local_recovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure_type: type[Exception]
) -> None:
    from auto_zettelkasten import pipeline

    content = {
        "text": "", "content_hash": "hash", "source_file": str(tmp_path / "custody.pdf"),
        "content_route": "codex_pdf_page_images", "media_type": "application/pdf",
        "source_scope": "full_document", "source_coverage": {}, "coverage_reason": "image_only",
        "coverage_metrics": {}, "document_route": {"recovery": {"state": "not_selected"}, "identity": "id"},
    }
    monkeypatch.setattr(pipeline, "_load_frozen_content", lambda *_args: dict(content))
    monkeypatch.setattr(pipeline, "_compatible_committed_note", lambda *_args, **_kwargs: None)
    reads = 0
    recoveries = 0

    def read(*_args, **_kwargs):
        nonlocal reads
        reads += 1
        if reads == 1:
            raise failure_type("recoverable")
        raise ProviderQuotaExhausted("stop after proving one recovery")

    def recover(value, *_args, **_kwargs):
        nonlocal recoveries
        recoveries += 1
        recovered = dict(value)
        recovered["content_route"] = "local_ocr_after_codex_image_recovery"
        return recovered

    monkeypatch.setattr(pipeline, "_read_document", read)
    monkeypatch.setattr(pipeline, "_recover_pdf_image_route", recover)
    reader = SimpleNamespace(name="codex", model="gpt-5.6-luna", is_cloud=True)
    item = {"key": "A1", "data": {"key": "A1", "itemType": "journalArticle", "title": "A"}}
    with pytest.raises(ProviderQuotaExhausted):
        _prepare_item(
            tmp_path, tmp_path / "run", 0, item,
                _request(tmp_path),
            SimpleNamespace(), reader, None,
        )
    assert reads == 2 and recoveries == 1


@pytest.mark.parametrize(
    "failure",
    [ProviderQuotaExhausted("quota"), CloudPermissionError("auth"), ProviderIsolationFailure("isolation")],
)
def test_quota_auth_and_isolation_never_select_local_recovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: Exception
) -> None:
    from auto_zettelkasten import pipeline

    content = {
        "text": "", "content_hash": "hash", "source_file": str(tmp_path / "custody.pdf"),
        "content_route": "codex_pdf_page_images", "media_type": "application/pdf",
        "source_scope": "full_document", "source_coverage": {}, "coverage_reason": "image_only",
        "coverage_metrics": {}, "document_route": {"recovery": {"state": "not_selected"}, "identity": "id"},
    }
    monkeypatch.setattr(pipeline, "_load_frozen_content", lambda *_args: dict(content))
    monkeypatch.setattr(pipeline, "_compatible_committed_note", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(pipeline, "_read_document", lambda *_args, **_kwargs: (_ for _ in ()).throw(failure))
    monkeypatch.setattr(
        pipeline, "_recover_pdf_image_route", lambda *_args, **_kwargs: pytest.fail("recovery selected"),
    )
    reader = SimpleNamespace(name="codex", model="gpt-5.6-luna", is_cloud=True)
    item = {"key": "A1", "data": {"key": "A1", "itemType": "journalArticle", "title": "A"}}
    request = _request(tmp_path)
    if isinstance(failure, ProviderQuotaExhausted):
        with pytest.raises(ProviderQuotaExhausted):
            _prepare_item(tmp_path, tmp_path / "run", 0, item, request, SimpleNamespace(), reader, None)
    else:
        result = _prepare_item(tmp_path, tmp_path / "run", 0, item, request, SimpleNamespace(), reader, None)
        assert result["terminal_status"] == "parked_for_review"
