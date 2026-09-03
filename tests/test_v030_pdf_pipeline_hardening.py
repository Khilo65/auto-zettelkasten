from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_zettelkasten.extraction import (
    ContentAdequacy,
    ContentAdequacyClass,
    ExtractionResult,
    PDFPageEvidence,
    PDFStructuralProbe,
)
from auto_zettelkasten.files import read_yaml, sha256_file, write_yaml
from auto_zettelkasten.literature import _CheckpointedReasonerCalls
from auto_zettelkasten.models import (
    ExtractionPolicy,
    LiteratureMapRequest,
    LiteratureMappingPolicy,
    MapRequest,
)
from auto_zettelkasten.pipeline import (
    _codex_pdf_input_file_preflight,
    _custodied_pdf_candidate,
    _load_pdf_local_recovery_cache,
    _read_document,
    _recover_pdf_image_route,
    _source_replay_request_hash,
    source_replay_receipt_matches,
)
from auto_zettelkasten.relationships import stable_hash
from auto_zettelkasten.readers import codex_stage_identity


def _request(
    workspace: Path,
    *,
    languages: tuple[str, ...] = ("eng",),
    pdf_fallback: str = "ocr",
) -> MapRequest:
    return MapRequest(
        workspace,
        provider="codex",
        model="gpt-5.6-luna",
        literature_model="gpt-5.6-terra",
        allow_cloud=True,
        extraction_policy=ExtractionPolicy(
            pdf_fallback=pdf_fallback, languages=languages
        ),
    )


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


def _probe(document: bytes) -> PDFStructuralProbe:
    adequacy = ContentAdequacy(
        ContentAdequacyClass.PARTIAL_PDF_TEXT,
        "fulltext_available",  # type: ignore[arg-type]
        "limited",
        "image_only",
        metrics={"page_count": 1},
    )
    text = "Sparse embedded text"
    page = PDFPageEvidence(
        1,
        "1",
        612,
        792,
        text,
        hashlib.sha256(text.encode()).hexdigest(),
        len(text),
        3,
        "suspicious",
        ("/Image",),
        1,
        1,
        1,
        True,
        True,
        True,
    )
    return PDFStructuralProbe(
        "succeeded",
        "",
        "application/pdf",
        hashlib.sha256(document).hexdigest(),
        len(document),
        1,
        (page,),
        text,
        adequacy,
        {},
        (1,),
        (1,),
        ("1",),
    )


def _raw_route(custody: Path) -> dict:
    payload = {
        "route_version": "1",
        "route": "codex_pdf_input_file",
        "custody_file": str(custody.resolve()),
        "custody_sha256": hashlib.sha256(custody.read_bytes()).hexdigest(),
        "model_profile": {
            "model": "gpt-5.6-luna",
            "reasoning_effort": "medium",
            "cli_version": "0.152.1",
        },
    }
    return {
        "identity_payload": payload,
        "identity": stable_hash(payload),
        "recovery": {"state": "not_selected"},
    }


def test_raw_pdf_preflight_counts_each_input_component_once(monkeypatch) -> None:
    from auto_zettelkasten import pipeline

    def fake_preflight(text, _metadata, _question, dimensions):
        return {
            "document_input_tokens": 100 + len(text),
            "image_tokens": 50 if dimensions else 0,
        }

    monkeypatch.setattr(pipeline, "codex_source_bundle_image_preflight", fake_preflight)
    result = _codex_pdf_input_file_preflight("x" * 20, {}, None, [(612, 792)])

    assert result["prompt_text_tokens"] == 100
    assert result["pdf_extracted_text_tokens"] == 20
    assert result["image_tokens"] == 50
    assert result["document_input_tokens"] == 170
    assert result["uncertainty_tokens"] == 16_384
    assert result["combined_tokens"] == 82_090


def test_raw_pdf_checkpoint_uses_0152_identity_and_replays_without_reader(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    custody_root = workspace / "01_custody" / "files"
    custody_root.mkdir(parents=True)
    custody = custody_root / "source.pdf"
    custody.write_bytes(b"%PDF-1.7\n%%EOF\n")
    route = _raw_route(custody)
    checkpoint = tmp_path / "checkpoint"

    class Reader:
        name = "codex"
        model = "gpt-5.6-luna"
        context_window_tokens = 272_000

        def read_source_bundle(self, *_args, attachment_paths=(), **_kwargs):
            assert attachment_paths == (custody,)
            return _bundle()

    first = _read_document(
        Reader(),
        "",
        {"_source_context": {"source_id": "source-zotero-A1", "zotero_key": "A1"}},
        None,
        request=_request(workspace),
        checkpoint_root=checkpoint,
        document_route=route,
        expected_custody_hash=hashlib.sha256(custody.read_bytes()).hexdigest(),
        expected_custody_file=custody,
        custody_root=custody_root,
    )
    identity = read_yaml(checkpoint / "direct.yml")["identity"]
    assert identity["provider_execution_identity"]["source_bundle"]["cli_profile"] == (
        "0.152.1"
    )

    class ReplayReader(Reader):
        def read_source_bundle(self, *_args, **_kwargs):
            pytest.fail("durable raw-PDF checkpoint must replay without a provider")

    second = _read_document(
        ReplayReader(),
        "",
        {"_source_context": {"source_id": "source-zotero-A1", "zotero_key": "A1"}},
        None,
        request=_request(workspace),
        checkpoint_root=checkpoint,
        document_route=route,
        expected_custody_hash=hashlib.sha256(custody.read_bytes()).hexdigest(),
        expected_custody_file=custody,
        custody_root=custody_root,
    )
    assert second[0] == first[0]
    assert second[2] == "reused_direct_source_checkpoint"


def test_non_pdf_call_uses_cached_0152_identity_and_replays_without_preflight(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    checkpoint = tmp_path / "checkpoint"

    class Reader:
        name = "codex"
        model = "gpt-5.6-luna"
        context_window_tokens = 272_000
        _preflight = None
        preflight_calls = 0

        def _ensure_codex_preflight(self):
            self.preflight_calls += 1
            self._preflight = {"version": "0.152.1"}

        def read_source_bundle(self, *_args, attachment_paths=(), **_kwargs):
            assert not attachment_paths
            return _bundle()

    reader = Reader()
    first = _read_document(
        reader,
        "Plain source text.",
        {"_source_context": {"source_id": "source-zotero-A1", "zotero_key": "A1"}},
        None,
        request=_request(workspace),
        checkpoint_root=checkpoint,
    )
    assert reader.preflight_calls == 1
    identity = read_yaml(checkpoint / "direct.yml")["identity"]
    assert identity["provider_execution_identity"]["source_bundle"]["cli_profile"] == (
        "0.152.1"
    )

    class ReplayReader(Reader):
        def _ensure_codex_preflight(self):
            pytest.fail("checkpoint replay must not preflight the helper")

        def read_source_bundle(self, *_args, **_kwargs):
            pytest.fail("checkpoint replay must not call the provider")

    second = _read_document(
        ReplayReader(),
        "Plain source text.",
        {"_source_context": {"source_id": "source-zotero-A1", "zotero_key": "A1"}},
        None,
        request=_request(workspace),
        checkpoint_root=checkpoint,
    )
    assert second[0] == first[0]
    assert second[2] == "reused_direct_source_checkpoint"


def test_stale_0145_checkpoint_cannot_label_new_0152_call(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    checkpoint = tmp_path / "checkpoint"

    class Reader:
        name = "codex"
        model = "gpt-5.6-luna"
        context_window_tokens = 272_000

        def __init__(self, version: str) -> None:
            self._preflight = {"version": version}

        def read_source_bundle(self, *_args, **_kwargs):
            return _bundle()

    metadata = {
        "_source_context": {"source_id": "source-zotero-A1", "zotero_key": "A1"}
    }
    _read_document(
        Reader("0.145.0"),
        "Old source text.",
        metadata,
        None,
        request=_request(workspace),
        checkpoint_root=checkpoint,
    )
    _read_document(
        Reader("0.152.1"),
        "Changed source text.",
        metadata,
        None,
        request=_request(workspace),
        checkpoint_root=checkpoint,
    )

    identity = read_yaml(checkpoint / "direct.yml")["identity"]
    assert identity["provider_execution_identity"]["source_bundle"]["cli_profile"] == (
        "0.152.1"
    )


def test_graph_checkpoint_uses_0152_and_replays_without_preflight(
    tmp_path: Path,
) -> None:
    class Reasoner:
        name = "codex"
        model = "gpt-5.6-terra"
        _preflight = {"version": "0.152.1"}

        def propose_clusters(self, *_args, **_kwargs):
            return {"clusters": []}

    request = LiteratureMapRequest(
        workspace=tmp_path,
        run_id="identity-run",
        provider="codex",
        model="gpt-5.6-terra",
        reasoning_effort="medium",
        allow_cloud=True,
        literature_policy=LiteratureMappingPolicy(max_synthesis_calls=1),
    )
    first = _CheckpointedReasonerCalls(
        tmp_path, "identity-run", Reasoner(), request
    )
    assert first(
        "cluster_proposal", "all", "propose_clusters", [], {}
    ) == {"clusters": []}
    checkpoint = read_yaml(
        tmp_path
        / "11_state"
        / "runs"
        / "identity-run"
        / "literature"
        / "synthesis"
        / "cluster_proposal"
        / "all.yml"
    )
    assert checkpoint["dependency_component_hashes"][
        "provider_execution_identity"
    ] == stable_hash(
        codex_stage_identity(
            "cluster_proposal",
            "gpt-5.6-terra",
            "medium",
            cli_profile="0.152.1",
        )
    )

    class ReplayReasoner(Reasoner):
        _preflight = None

        def _ensure_codex_preflight(self):
            pytest.fail("graph checkpoint replay must not preflight Codex")

        def propose_clusters(self, *_args, **_kwargs):
            pytest.fail("graph checkpoint replay must not call the provider")

    replay = _CheckpointedReasonerCalls(
        tmp_path, "identity-run", ReplayReasoner(), request
    )
    assert replay(
        "cluster_proposal", "all", "propose_clusters", [], {}
    ) == {"clusters": []}
    assert replay.provider_calls == 0
    assert replay.checkpoint_hits == 1


def test_recovered_ocr_is_consumed_only_by_matching_new_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from auto_zettelkasten import pipeline

    workspace = tmp_path / "workspace"
    custody_root = workspace / "01_custody" / "files"
    custody_root.mkdir(parents=True)
    document = b"%PDF-1.7\nscanned\n%%EOF\n"
    custody = custody_root / "source.pdf"
    custody.write_bytes(document)
    route = _raw_route(custody)
    content = {
        "text": "",
        "content_hash": hashlib.sha256(document).hexdigest(),
        "source_file": str(custody),
        "content_route": "codex_pdf_input_file",
        "media_type": "application/pdf",
        "source_scope": "full_document",
        "source_coverage": {},
        "coverage_reason": "image_only",
        "coverage_metrics": {"page_count": 1},
        "document_route": route,
    }
    recovered_adequacy = ContentAdequacy(
        ContentAdequacyClass.FULL_PDF_TEXT,
        "full_document",
        "passed",
        "full_pdf_text",
        metrics={"page_count": 1, "recovered_pages": [1]},
    )
    monkeypatch.setattr(
        pipeline,
        "extract_path",
        lambda *_args, **_kwargs: ExtractionResult(
            status="succeeded",
            text="Locally recovered source text.",
            route="tesseract_ocr",
            media_type="application/pdf",
            page_count=1,
            adequacy=recovered_adequacy,
        ),
    )
    old_run_checkpoint = workspace / "11_state" / "runs" / "old" / "items" / "A1"
    recovered = _recover_pdf_image_route(
        content,
        _request(workspace),
        workspace,
        old_run_checkpoint,
        trigger="ProviderUnsupportedAttachment",
        acquisition_gate=None,
        cancel_event=None,
    )
    assert recovered["text"] == "Locally recovered source text."
    assert (
        _load_pdf_local_recovery_cache(
            custody,
            hashlib.sha256(document).hexdigest(),
            _request(workspace, languages=("fra",)),
        )
        is None
    )

    monkeypatch.setattr(
        pipeline, "probe_pdf_bytes", lambda *_args, **_kwargs: _probe(document)
    )
    reader = SimpleNamespace(
        name="codex",
        model="gpt-5.6-luna",
        pdf_input_file_status=lambda: pytest.fail(
            "matching recovery must bypass raw-PDF admission"
        ),
    )
    candidate, extracted = _custodied_pdf_candidate(
        document,
        custody,
        {},
        {"key": "A1", "data": {"key": "A1", "itemType": "journalArticle"}},
        {"source_id": "source-zotero-A1"},
        _request(workspace),
        actual_primary_pdf=True,
        cancelled=None,
        reader=reader,
    )
    assert extracted.route == "tesseract_ocr_after_codex_pdf_recovery"
    assert candidate is not None
    assert candidate["text"] == "Locally recovered source text."
    assert "document_route" not in candidate

    calls = 0

    class TextReader:
        name = "codex"
        model = "gpt-5.6-luna"
        context_window_tokens = 272_000
        _preflight = {"version": "0.152.1"}

        def read_source_bundle(self, *_args, attachment_paths=(), **_kwargs):
            nonlocal calls
            calls += 1
            assert not attachment_paths
            return _bundle()

    _read_document(
        TextReader(),
        candidate["text"],
        {"_source_context": {"source_id": "source-zotero-A1", "zotero_key": "A1"}},
        None,
        request=_request(workspace),
        checkpoint_root=workspace / "11_state" / "runs" / "new" / "items" / "A1",
    )
    assert calls == 1


def test_legacy_default_pdf_fallback_receipt_matches_without_writes(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    run_id = "legacy"
    run_root = workspace / "11_state" / "runs" / run_id
    run_root.mkdir(parents=True)
    inventory = run_root / "inventory.json"
    inventory.write_text("[]\n", encoding="utf-8")
    request = _request(workspace, pdf_fallback="none")
    receipt = run_root / "source_replay_receipt.yml"
    write_yaml(
        receipt,
        {
            "receipt_schema_version": "1",
            "engine_version": "0.30.0",
            "artifact_schema_version": "1.20",
            "request_hash": _source_replay_request_hash(
                request, omit_default_pdf_fallback=True
            ),
            "dependencies": [
                {
                    "path": str(inventory.relative_to(workspace)),
                    "sha256": sha256_file(inventory),
                }
            ],
        },
    )
    before = {
        path: (sha256_file(path), path.stat().st_mtime_ns)
        for path in (inventory, receipt)
    }

    assert source_replay_receipt_matches(workspace, run_id, request, {"items": []})
    assert {
        path: (sha256_file(path), path.stat().st_mtime_ns)
        for path in (inventory, receipt)
    } == before


def test_raw_route_does_not_embed_probe_text_in_provider_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from auto_zettelkasten import pipeline

    workspace = tmp_path / "workspace"
    custody = workspace / "01_custody" / "files" / "source.pdf"
    custody.parent.mkdir(parents=True)
    document = b"%PDF-1.7\n%%EOF\n"
    custody.write_bytes(document)
    monkeypatch.setattr(
        pipeline, "probe_pdf_bytes", lambda *_args, **_kwargs: _probe(document)
    )
    reader = SimpleNamespace(
        name="codex",
        model="gpt-5.6-luna",
        pdf_input_file_status=lambda: {
            "version": "0.152.1",
            "pdf_input_file_capability": True,
            "_helper_manifest_identity": {"manifest_sha256": "a" * 64},
        },
    )
    candidate, extracted = _custodied_pdf_candidate(
        document,
        custody,
        {},
        {"key": "A1", "data": {"key": "A1", "itemType": "journalArticle"}},
        {"source_id": "source-zotero-A1"},
        MapRequest(
            workspace,
            provider="codex",
            model="gpt-5.6-luna",
            literature_model="gpt-5.6-terra",
            allow_cloud=True,
        ),
        actual_primary_pdf=True,
        cancelled=None,
        reader=reader,
    )
    assert extracted.text == "Sparse embedded text"
    assert candidate is not None and candidate["text"] == ""
    estimate = candidate["document_route"]["identity_payload"]["projected_preflight"]
    assert estimate["pdf_extracted_text_tokens"] > 0
