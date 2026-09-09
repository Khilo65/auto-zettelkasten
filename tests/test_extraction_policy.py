from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from auto_zettelkasten.cli import _extraction_policy, build_parser, main
from auto_zettelkasten.files import read_yaml, sha256_bytes
from auto_zettelkasten.extraction import ExtractionResult, classify_pdf_text
from auto_zettelkasten.models import ExtractionPolicy, MapRequest
from auto_zettelkasten.pipeline import (
    _acquire_content,
    _attachment_candidate_rank,
    _indexed_pdf_text_with_page_markers,
)
from auto_zettelkasten.workspace import initialize


def test_extraction_policy_is_serializable_and_validated(tmp_path: Path) -> None:
    request = MapRequest(
        tmp_path,
        extraction_policy=ExtractionPolicy(
            ocr="required",
            languages=("eng", "ara", "eng"),
            pdf_fallback="ocr",
        ),
    )

    restored = MapRequest.from_dict(request.to_dict())

    assert restored == request
    assert restored.extraction_policy.languages == ("eng", "ara")
    assert restored.extraction_version == "2"
    assert restored.prompt_version == "15"
    with pytest.raises(ValueError, match="auto, off, or required"):
        ExtractionPolicy(ocr="sometimes")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="language code"):
        ExtractionPolicy(languages=("eng;rm -rf",))
    with pytest.raises(ValueError, match="none, images, or ocr"):
        ExtractionPolicy(pdf_fallback="automatic")  # type: ignore[arg-type]


def test_workspace_and_cli_extraction_precedence(tmp_path: Path) -> None:
    initialize(tmp_path)
    config = read_yaml(tmp_path / "auto-zettelkasten.yml")
    assert config["extraction"] == {
        "version": "2",
        "ocr": "auto",
        "languages": ["eng"],
        "pdf_fallback": "none",
        "vision": "configured_only",
    }
    assert config["prompt_version"] == "15"

    args = build_parser().parse_args(
        [
            "map",
            "--workspace",
            str(tmp_path),
            "--ocr",
            "required",
            "--ocr-language",
            "eng",
            "--ocr-language",
            "ara",
            "--pdf-fallback",
            "images",
        ]
    )
    assert _extraction_policy(args, config) == ExtractionPolicy(
        ocr="required", languages=("eng", "ara"), pdf_fallback="images"
    )

    sync_args = build_parser().parse_args(
        [
            "sync",
            "--workspace",
            str(tmp_path),
            "--pdf-fallback",
            "ocr",
        ]
    )
    assert _extraction_policy(sync_args, config).pdf_fallback == "ocr"


@pytest.mark.parametrize(
    ("command", "target"),
    [("map", "run_map"), ("sync", "sync_zotero")],
)
def test_map_and_sync_forward_explicit_pdf_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    command: str,
    target: str,
) -> None:
    initialize(tmp_path)
    captured: list[MapRequest] = []

    def run(request: MapRequest, **_kwargs):
        captured.append(request)
        return (
            SimpleNamespace(to_dict=lambda: {"status": "completed"})
            if command == "map"
            else {"status": "completed"}
        )

    monkeypatch.setattr(f"auto_zettelkasten.cli.{target}", run)
    assert (
        main(
            [
                command,
                "--workspace",
                str(tmp_path),
                "--pdf-fallback",
                "images",
            ]
        )
        == 0
    )
    assert captured[0].extraction_policy.pdf_fallback == "images"


def test_actual_primary_pdf_outranks_index_and_supplement() -> None:
    parent = {
        "title": "Mediation in Internationalized Civil Wars",
        "DOI": "10.1234/main",
    }
    main = {
        "title": "Full Text PDF",
        "filename": "Mediation in Internationalized Civil Wars.pdf",
        "DOI": "10.1234/main",
    }
    supplement = {
        "title": "Supporting information",
        "filename": "mediation-dataset.pdf",
    }

    indexed_rank = _attachment_candidate_rank(
        main, parent, media_type="application/pdf", actual_file=False
    )
    actual_rank = _attachment_candidate_rank(
        main, parent, media_type="application/pdf", actual_file=True
    )
    supplement_rank = _attachment_candidate_rank(
        supplement, parent, media_type="application/pdf", actual_file=True
    )

    assert indexed_rank is not None
    assert actual_rank is not None
    assert supplement_rank is not None
    assert actual_rank > indexed_rank > supplement_rank


def test_html_supplements_and_reviewer_responses_do_not_outrank_main_text() -> None:
    parent = {"title": "Mediation in Internationalized Civil Wars"}

    assert _attachment_candidate_rank(
        {"title": "Full text"},
        parent,
        media_type="text/html",
        actual_file=True,
    ) == 90
    assert _attachment_candidate_rank(
        {"title": "Supporting information"},
        parent,
        media_type="text/html",
        actual_file=True,
    ) == 60
    assert _attachment_candidate_rank(
        {"title": "Supporting information"},
        parent,
        media_type="text/html",
        actual_file=False,
    ) == 50
    assert _attachment_candidate_rank(
        {"title": "Response to Reviewers"},
        parent,
        media_type="text/html",
        actual_file=True,
    ) == 40


def test_sparse_actual_pdf_metadata_still_outranks_its_indexed_text() -> None:
    parent = {
        "title": "Mediator Flexibility and Institutional Constraints",
        "DOI": "10.1234/article",
    }
    attachment = {
        "title": "PDF",
        "filename": "download-7f8c4a.pdf",
    }

    actual_rank = _attachment_candidate_rank(
        attachment, parent, media_type="application/pdf", actual_file=True
    )
    indexed_rank = _attachment_candidate_rank(
        attachment, parent, media_type="application/pdf", actual_file=False
    )

    assert actual_rank is not None
    assert indexed_rank is not None
    assert actual_rank > indexed_rank


@pytest.mark.parametrize(
    "label",
    ["Cover Letter", "Response to Reviewers", "Editorial Decision"],
)
def test_administrative_pdf_is_not_selected_as_the_primary_publication(
    label: str,
) -> None:
    parent = {
        "title": "Mediation in Internationalized Civil Wars",
        "DOI": "10.1234/main",
    }
    attachment = {
        "title": label,
        "filename": f"{label}.pdf",
    }

    actual_rank = _attachment_candidate_rank(
        attachment, parent, media_type="application/pdf", actual_file=True
    )
    indexed_rank = _attachment_candidate_rank(
        attachment, parent, media_type="application/pdf", actual_file=False
    )

    assert actual_rank is not None and actual_rank < 100
    assert indexed_rank is not None and indexed_rank < 100


def test_indexed_pdf_fallback_requires_explicit_page_boundaries() -> None:
    coverage = {"indexedPages": 2, "totalPages": 2}

    assert _indexed_pdf_text_with_page_markers("first\fsecond", coverage) == (
        "--- Page 1 ---\nfirst\n\n--- Page 2 ---\nsecond"
    )
    assert _indexed_pdf_text_with_page_markers("first second", coverage) is None
    assert (
        _indexed_pdf_text_with_page_markers(
            "--- Page 1 ---\nfirst\n--- Page 2 ---\nsecond", coverage
        )
        is not None
    )


def test_acquisition_prefers_actual_primary_pdf_over_complete_zotero_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    initialize(workspace)
    local_pdf = tmp_path / "main.pdf"
    local_pdf.write_bytes(b"synthetic-pdf")
    parent = {
        "key": "ITEM1",
        "data": {
            "key": "ITEM1",
            "itemType": "journalArticle",
            "title": "Primary Article",
            "DOI": "10.1234/main",
        },
    }
    child = {
        "key": "PDF1",
        "data": {
            "key": "PDF1",
            "parentItem": "ITEM1",
            "itemType": "attachment",
            "title": "Primary Article Full Text",
            "filename": "Primary Article.pdf",
            "contentType": "application/pdf",
            "local_path": str(local_pdf),
            "DOI": "10.1234/main",
        },
    }
    actual_text = "\n\n".join(
        f"--- Page {page} ---\n" + (f"actual page {page} evidence " * 80)
        for page in (1, 2)
    )

    class Zotero:
        def children(self, item_key: str):
            return [child]

        def fulltext(self, item_key: str):
            if item_key == "ITEM1":
                return {
                    "content": (
                        "<html><body><div class='abstract'>Abstract: This study "
                        "examines mediation outcomes in two conflicts.</div>"
                        "<p>You do not currently have access to this article.</p>"
                        "</body></html>"
                    ),
                    "contentType": "text/html",
                }
            if item_key != "PDF1":
                return None
            return {
                "content": ("indexed first page " * 120)
                + "\f"
                + ("indexed second page " * 120),
                "contentType": "application/pdf",
                "indexedPages": 2,
                "totalPages": 2,
            }

        def file(self, item_key: str):
            return None

    monkeypatch.setattr(
        "auto_zettelkasten.pipeline.extract_pdf_from_probe",
        lambda *args, **kwargs: ExtractionResult(
            status="succeeded",
            text=actual_text,
            route="pypdf_text",
            media_type="application/pdf",
            page_count=2,
            adequacy=classify_pdf_text(actual_text, page_count=2),
        ),
    )

    content = _acquire_content(
        workspace,
        parent,
        Zotero(),  # type: ignore[arg-type]
        {
            "attempts": [],
            "source_id": "source-zotero-item1",
            "zotero_item_key": "ITEM1",
        },
        MapRequest(workspace, provider="ollama", model="fake"),
        None,
    )

    assert content is not None
    assert content["content_route"] == "pypdf_text"
    assert content["text"] == actual_text
    assert Path(content["source_file"]).is_relative_to(
        workspace / "01_custody" / "files"
    )

    monkeypatch.setattr(
        "auto_zettelkasten.pipeline.extract_pdf_from_probe",
        lambda *args, **kwargs: ExtractionResult(
            status="failed",
            text="--- Page 1 ---\n\n--- Page 2 ---",
            route="pypdf_text",
            reason="required_ocr_unavailable",
            media_type="application/pdf",
            page_count=2,
            # Density can pass before a later page-level recovery decision
            # rejects the document; the limited candidate must still normalize
            # the authoritative coverage gate to failed.
            adequacy=classify_pdf_text(actual_text, page_count=2),
        ),
    )
    failed = _acquire_content(
        workspace,
        parent,
        Zotero(),  # type: ignore[arg-type]
        {
            "attempts": [],
            "source_id": "source-zotero-item1",
            "zotero_item_key": "ITEM1",
        },
        MapRequest(
            workspace,
            provider="ollama",
            model="fake",
            extraction_policy=ExtractionPolicy(ocr="required"),
        ),
        None,
    )
    assert failed is not None
    assert failed["source_scope"] == "abstract_only"
    assert failed["source_coverage"]["coverage_gate"] == "limited"
    assert failed["coverage_reason"] == "primary_pdf_unreadable_abstract_available"
    assert "examines mediation outcomes" in failed["text"]
    assert failed["content_route"] != "zotero_fulltext"


def test_acquisition_prefers_full_raw_html_with_selected_state(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    initialize(workspace)
    paragraph = (
        "Comparative country evidence reports reputation measures, rankings, "
        "and respondent results for interpretation. " * 35
    )
    indexed_blocks = "".join(f"<p>{paragraph}</p>" for _ in range(5))
    raw_blocks = "".join(f"<p>{paragraph}</p>" for _ in range(4))
    indexed_html = (
        f"<html><body><h1>Nation results</h1>{indexed_blocks}</body></html>"
    )
    raw_html = f"""
    <html><body>
      <select><option>France<option selected>Israel<option>Italy</select>
      <select><option>2023<option selected>2024</select>
      <select><option selected>Global<option>Business</select>
      <h1>Nation results</h1>{raw_blocks}
    </body></html>
    """
    parent = {
        "key": "ITEM1",
        "data": {
            "key": "ITEM1",
            "itemType": "webpage",
            "title": "Nation Results",
        },
    }
    child = {
        "key": "HTML1",
        "data": {
            "key": "HTML1",
            "parentItem": "ITEM1",
            "itemType": "attachment",
            "title": "Nation Results",
            "filename": "nation.html",
            "contentType": "text/html",
        },
    }

    class Zotero:
        def children(self, item_key: str):
            return [child]

        def fulltext(self, item_key: str):
            if item_key == "HTML1":
                return {"content": indexed_html, "contentType": "text/html"}
            return None

        def file(self, item_key: str):
            if item_key == "HTML1":
                return raw_html.encode(), "text/html"
            return None

    content = _acquire_content(
        workspace,
        parent,
        Zotero(),  # type: ignore[arg-type]
        {
            "attempts": [],
            "source_id": "source-zotero-item1",
            "zotero_item_key": "ITEM1",
        },
        MapRequest(workspace, provider="ollama", model="fake"),
        None,
    )

    assert content is not None
    assert content["content_route"] == "html_text"
    assert content["content_hash"] == sha256_bytes(raw_html.encode())
    assert Path(content["source_file"]).is_relative_to(
        workspace / "01_custody" / "files"
    )
    assert "Selected option: Israel" in content["text"]
    assert "Selected option: 2024" in content["text"]
    assert "Selected option: Global" in content["text"]


@pytest.mark.parametrize("provider", ["codex", "deepseek"])
@pytest.mark.parametrize("numeral", ["~OO,OOO", "500,000"])
@pytest.mark.parametrize("fallback", ["unavailable", "raw", "abstract"])
def test_indexed_pdf_damage_cannot_bypass_local_recovery(
    monkeypatch, tmp_path, provider, numeral, fallback
):
    from auto_zettelkasten import extraction
    from auto_zettelkasten.files import sha256_text
    from test_pdf_recovery import _pdf, _prose

    text = _prose("indexed", 220) + f" Reported total {numeral}."
    clean_raw_text = _prose("raw", 220) + " Reported total 500,000."
    document = _pdf([clean_raw_text])
    parent = {
        "key": "PARENTA1",
        "data": {
            "key": "PARENTA1",
            "itemType": "journalArticle",
            "title": "Primary article",
        },
    }
    child = {
        "key": "PDFKEYA1",
        "data": {
            "key": "PDFKEYA1",
            "itemType": "attachment",
            "title": "Full text PDF",
            "filename": "article.pdf",
            "contentType": "application/pdf",
        },
    }
    abstract = "This study examines mediation outcomes and describes the relevant methods and evidence."

    class Zotero:
        def children(self, _key):
            return [child]

        def fulltext(self, key):
            if key == "PDFKEYA1":
                return {
                    "content": text,
                    "contentType": "application/pdf",
                    "indexedPages": 1,
                    "totalPages": 1,
                }
            if fallback == "abstract":
                return {
                    "content": f"<div class='abstract'>Abstract: {abstract}</div>",
                    "contentType": "text/html",
                }
            return None

        def file(self, key):
            return (
                (document, "application/pdf")
                if fallback == "raw" and key == "PDFKEYA1"
                else None
            )

    monkeypatch.setattr(
        extraction,
        "_ocr_pdf_page",
        lambda *_args: pytest.fail("clean raw PDF needs no OCR"),
    )
    base = {"source_id": "source-zotero-PARENTA1", "attempts": []}
    request = MapRequest(
        tmp_path,
        provider=provider,
        model="gpt-5.6-luna" if provider == "codex" else "deepseek-v4-flash",
        literature_model="gpt-5.6-terra" if provider == "codex" else None,
        allow_cloud=True,
        extraction_policy=ExtractionPolicy(pdf_fallback="ocr"),
    )
    result = _acquire_content(tmp_path, parent, Zotero(), base, request, None)
    rejected = [
        attempt
        for attempt in base["attempts"]
        if "indexed_pdf_damaged_numeral" in attempt["reason"]
    ]
    if numeral == "~OO,OOO":
        assert len(rejected) == 1
        assert rejected[0]["status"] == "failed" and rejected[0][
            "input_hash"
        ] == sha256_text(text)
        assert numeral not in result["text"]
        if fallback == "unavailable":
            assert result["source_scope"] == "metadata_only"
        elif fallback == "abstract":
            assert (
                result["source_scope"] == "abstract_only" and abstract in result["text"]
            )
    else:
        assert rejected == []
        assert result["source_scope"] == "full_document"
        if fallback != "raw":
            assert (
                result["content_route"] == "zotero_fulltext"
                and numeral in result["text"]
            )
    if fallback == "raw":
        assert (
            result["source_scope"] == "full_document"
            and result["content_route"] == "pypdf_text"
        )
        assert clean_raw_text in result["text"]
