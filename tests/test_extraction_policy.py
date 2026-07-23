from __future__ import annotations

from pathlib import Path

import pytest

from auto_zettelkasten.cli import _extraction_policy, build_parser
from auto_zettelkasten.files import read_yaml
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
            ocr="required", languages=("eng", "ara", "eng")
        ),
    )

    restored = MapRequest.from_dict(request.to_dict())

    assert restored == request
    assert restored.extraction_policy.languages == ("eng", "ara")
    assert restored.extraction_version == "2"
    assert restored.prompt_version == "8"
    with pytest.raises(ValueError, match="auto, off, or required"):
        ExtractionPolicy(ocr="sometimes")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="language code"):
        ExtractionPolicy(languages=("eng;rm -rf",))


def test_workspace_and_cli_extraction_precedence(tmp_path: Path) -> None:
    initialize(tmp_path)
    config = read_yaml(tmp_path / "auto-zettelkasten.yml")
    assert config["extraction"] == {
        "version": "2",
        "ocr": "auto",
        "languages": ["eng"],
        "vision": "configured_only",
    }
    assert config["prompt_version"] == "8"

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
        ]
    )
    assert _extraction_policy(args, config) == ExtractionPolicy(
        ocr="required", languages=("eng", "ara")
    )


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
        "auto_zettelkasten.pipeline.extract_path",
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
        "auto_zettelkasten.pipeline.extract_path",
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
