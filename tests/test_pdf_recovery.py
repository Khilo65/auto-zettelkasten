from __future__ import annotations

from pathlib import Path

import auto_zettelkasten.extraction as extraction


def test_normal_pdf_stays_on_fast_embedded_text_path(monkeypatch) -> None:
    def unexpected_ocr(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("ordinary embedded text must not invoke OCR")

    monkeypatch.setattr(extraction, "_ocr_pdf_page", unexpected_ocr)
    result = extraction.extract_bytes(
        _pdf([_prose("ordinary", 220)]),
        media_type="application/pdf",
        filename="ordinary.pdf",
    )

    assert result.status == "succeeded"
    assert result.route == "pypdf_text"
    assert result.text.startswith("--- Page 1 ---")
    assert result.coverage_metrics["embedded_text_page_count"] == 1
    assert result.coverage_metrics["ocr_page_count"] == 0


def test_repeated_sage_boilerplate_routes_every_page_to_ocr(monkeypatch) -> None:
    boilerplate = "Downloaded from SAGE. Please retry the article download. " * 12
    calls: list[int] = []

    def recover(_data: bytes, page_index: int, _languages: tuple[str, ...]):
        calls.append(page_index)
        labels = ("alpha", "bravo", "charlie", "delta")
        return extraction._OCRPageResult(
            text=_prose(labels[page_index], 70),
            route="pdfium_tesseract",
            available=True,
        )

    monkeypatch.setattr(extraction, "_ocr_pdf_page", recover)
    result = extraction.extract_bytes(
        _pdf([boilerplate] * 4),
        media_type="application/pdf",
        filename="touval-like.pdf",
    )

    assert result.status == "succeeded"
    assert calls == [0, 1, 2, 3]
    assert result.route == "pypdf_pdfium_tesseract"
    assert result.coverage_metrics["ocr_page_count"] == 4
    assert result.coverage_metrics["embedded_text_page_count"] == 0
    assert result.coverage_metrics["repeated_boilerplate_ratio"] >= 0.5
    assert [result.text.index(f"--- Page {page} ---") for page in range(1, 5)] == sorted(
        result.text.index(f"--- Page {page} ---") for page in range(1, 5)
    )


def test_mixed_pdf_ocrs_only_bad_page_and_preserves_good_pages(monkeypatch) -> None:
    first = _prose("embeddedalpha", 110)
    third = _prose("embeddedcharlie", 110)
    calls: list[int] = []

    def recover(_data: bytes, page_index: int, _languages: tuple[str, ...]):
        calls.append(page_index)
        return extraction._OCRPageResult(
            text=_prose("scannedbravo", 70),
            route="pdfium_tesseract",
            available=True,
        )

    monkeypatch.setattr(extraction, "_ocr_pdf_page", recover)
    result = extraction.extract_bytes(
        _pdf([first, "scan", third]),
        media_type="application/pdf",
        filename="mixed.pdf",
    )

    assert result.status == "succeeded"
    assert calls == [1]
    assert first in result.text
    assert third in result.text
    assert "scannedbravo" in result.text
    assert result.coverage_metrics["embedded_text_page_count"] == 2
    assert result.coverage_metrics["ocr_page_count"] == 1


def test_fully_scanned_pdf_recovers_all_pages_and_preserves_numbers(monkeypatch) -> None:
    calls: list[int] = []

    def recover(_data: bytes, page_index: int, _languages: tuple[str, ...]):
        calls.append(page_index)
        label = ("scanalpha", "scanbravo", "scancharlie")[page_index]
        numeric_detail = " The reported comparison was 500 fatalities against 1 in the reference period."
        return extraction._OCRPageResult(
            text=_prose(label, 75) + numeric_detail,
            route="pdfium_tesseract",
            available=True,
        )

    monkeypatch.setattr(extraction, "_ocr_pdf_page", recover)
    result = extraction.extract_bytes(
        _pdf(["", "", ""]),
        media_type="application/pdf",
        filename="fully-scanned.pdf",
    )

    assert result.status == "succeeded"
    assert calls == [0, 1, 2]
    assert result.coverage_metrics["ocr_page_count"] == 3
    assert result.text.count("500 fatalities against 1") == 3


def test_legitimate_blank_page_does_not_fail_substantive_document(monkeypatch) -> None:
    calls: list[int] = []

    def no_text(_data: bytes, page_index: int, _languages: tuple[str, ...]):
        calls.append(page_index)
        return extraction._OCRPageResult(
            available=True,
            route="pdfium_tesseract",
            nonprose_or_blank=True,
        )

    monkeypatch.setattr(extraction, "_ocr_pdf_page", no_text)
    pages = [_prose(label, 70) for label in ("alpha", "bravo", "charlie", "delta")]
    result = extraction.extract_bytes(
        _pdf([pages[0], "", pages[1], pages[2], pages[3]]),
        media_type="application/pdf",
        filename="report-with-divider.pdf",
    )

    assert result.status == "succeeded"
    assert calls == [1]
    assert result.coverage_metrics["unresolved_pages"] == ()
    assert result.coverage_metrics["page_routes"][1] == "nonprose_or_blank"
    assert "--- Page 2 ---" in result.text


def test_short_readable_page_is_kept_after_successful_ocr(monkeypatch) -> None:
    calls: list[int] = []

    def recover(_data: bytes, page_index: int, _languages: tuple[str, ...]):
        calls.append(page_index)
        return extraction._OCRPageResult(
            text="Introduction to Mediation",
            route="pdfium_tesseract",
            available=True,
        )

    monkeypatch.setattr(extraction, "_ocr_pdf_page", recover)
    pages = [_prose(label, 90) for label in ("alpha", "bravo", "charlie")]
    result = extraction.extract_bytes(
        _pdf(["scan", *pages]),
        media_type="application/pdf",
        filename="report-with-short-title.pdf",
    )

    assert result.status == "succeeded"
    assert calls == [0]
    assert "--- Page 1 ---\nIntroduction to Mediation" in result.text
    assert result.coverage_metrics["ocr_page_count"] == 1
    assert result.coverage_metrics["unresolved_pages"] == ()


def test_sparse_layout_is_not_skipped_before_ocr(tmp_path: Path) -> None:
    from PIL import Image, ImageDraw

    image_path = tmp_path / "divider.png"
    image = Image.new("L", (900, 1200), color=255)
    draw = ImageDraw.Draw(image)
    for top, width in ((430, 140), (490, 360), (550, 300)):
        draw.rectangle((450 - width // 2, top, 450 + width // 2, top + 12), fill=0)
    image.save(image_path)

    assert extraction._rendered_page_is_nonprose(image_path) is False
    assert (
        extraction._rendered_page_is_nonprose(
            image_path, allow_sparse_divider=True
        )
        is True
    )


def test_dense_unreadable_text_page_is_not_mistaken_for_a_divider(tmp_path: Path) -> None:
    from PIL import Image, ImageDraw

    image_path = tmp_path / "dense.png"
    image = Image.new("L", (900, 1200), color=255)
    draw = ImageDraw.Draw(image)
    for row in range(100, 900, 45):
        draw.rectangle((100, row, 800, row + 10), fill=0)
    image.save(image_path)

    assert extraction._rendered_page_is_nonprose(image_path) is False


def test_unavailable_ocr_never_silently_marks_a_page_complete(monkeypatch) -> None:
    monkeypatch.setattr(
        extraction, "_ocr_pdf_page", lambda *_args: extraction._OCRPageResult()
    )
    result = extraction.extract_bytes(
        _pdf([_prose("alpha", 120), "scan", _prose("charlie", 120)]),
        media_type="application/pdf",
        filename="unresolved-mixed.pdf",
    )

    assert result.status == "failed"
    assert result.reason == "unresolved_textual_pages"
    assert result.coverage_metrics["unresolved_pages"] == (2,)


def test_orientation_retry_uses_psm_one_when_psm_three_is_implausible(monkeypatch, tmp_path: Path) -> None:
    image = tmp_path / "page.png"
    image.write_bytes(b"not-read-by-test")
    calls: list[int] = []
    monkeypatch.setattr(extraction.shutil, "which", lambda command: "/usr/bin/tesseract" if command == "tesseract" else None)
    monkeypatch.setattr(extraction, "_render_pdf_page", lambda *_args: (image, "pdfium"))

    def fake_tesseract(_binary: str, _image: Path, *, language: str, psm: int) -> str:
        assert language == "eng"
        calls.append(psm)
        return "x" if psm == 3 else _prose("rotated", 70)

    monkeypatch.setattr(extraction, "_run_tesseract", fake_tesseract)
    recovered = extraction._ocr_pdf_page(b"pdf", 0, ("eng",))

    assert calls == [3, 1]
    assert recovered.available is True
    assert recovered.retry_used is True
    assert "rotated" in recovered.text


def test_landscape_pixel_rotation_retries_even_when_first_pass_has_many_words(
    monkeypatch, tmp_path: Path
) -> None:
    from PIL import Image

    image = tmp_path / "rotated.png"
    Image.new("L", (2200, 1700), color=255).save(image)
    calls: list[int] = []
    monkeypatch.setattr(
        extraction.shutil,
        "which",
        lambda command: "/usr/bin/tesseract" if command == "tesseract" else None,
    )
    monkeypatch.setattr(
        extraction, "_render_pdf_page", lambda *_args: (image, "pdfium")
    )

    def fake_tesseract(
        _binary: str, _image: Path, *, language: str, psm: int
    ) -> str:
        assert language == "eng"
        calls.append(psm)
        return (
            _prose("rotated-gibberish", 100)
            if psm == 3
            else _prose("upright-mediation", 80)
        )

    monkeypatch.setattr(extraction, "_run_tesseract", fake_tesseract)
    recovered = extraction._ocr_pdf_page(b"pdf", 0, ("eng",))

    assert calls == [3, 1]
    assert recovered.retry_used is True
    assert "upright-mediation" in recovered.text


def test_non_latin_ocr_text_passes_the_page_sniff() -> None:
    arabic = " ".join(
        ["تشرح", "الوساطة", "الأدلة", "والنتائج", "والسياق", "والمنهج"] * 12
    )

    assert len(extraction._alphabetic_words(arabic)) >= 60
    assert extraction._page_text_is_suspicious(arabic) is False


def test_plausible_ocr_first_pass_skips_orientation_retry(monkeypatch, tmp_path: Path) -> None:
    image = tmp_path / "page.png"
    image.write_bytes(b"not-read-by-test")
    calls: list[int] = []
    monkeypatch.setattr(extraction.shutil, "which", lambda command: "/usr/bin/tesseract" if command == "tesseract" else None)
    monkeypatch.setattr(extraction, "_render_pdf_page", lambda *_args: (image, "pdfium"))

    def fake_tesseract(_binary: str, _image: Path, *, language: str, psm: int) -> str:
        assert language == "eng"
        calls.append(psm)
        return _prose("upright", 70)

    monkeypatch.setattr(extraction, "_run_tesseract", fake_tesseract)
    recovered = extraction._ocr_pdf_page(b"pdf", 0, ("eng",))

    assert calls == [3]
    assert recovered.available is True
    assert recovered.retry_used is False
    assert "upright" in recovered.text


def test_flattened_table_text_and_important_numbers_remain_available(monkeypatch) -> None:
    def unexpected_ocr(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("flattened but substantive table text must not invoke OCR")

    monkeypatch.setattr(extraction, "_ocr_pdf_page", unexpected_ocr)
    table_text = (
        "Table one Treatment Control Fatalities 500 1 Confidence interval 95 percent. "
        "The surrounding discussion explains that these are descriptive counts and not an identified causal effect. "
        * 12
    )
    result = extraction.extract_bytes(
        _pdf([table_text]),
        media_type="application/pdf",
        filename="flattened-table.pdf",
    )

    assert result.status == "succeeded"
    assert "Fatalities 500 1" in result.text
    assert result.coverage_metrics["ocr_page_count"] == 0


def test_ocr_off_never_calls_recovery(monkeypatch) -> None:
    def unexpected_ocr(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("ocr=off must not invoke OCR")

    monkeypatch.setattr(extraction, "_ocr_pdf_page", unexpected_ocr)
    result = extraction.extract_bytes(
        _pdf(["Downloaded from SAGE. Please retry the article download. " * 12] * 4),
        media_type="application/pdf",
        filename="blocked.pdf",
        ocr_mode="off",
    )

    assert result.status == "failed"
    assert result.route == "pypdf_text"


def _prose(label: str, words: int) -> str:
    vocabulary = (
        f"{label} evidence describes mediation process actors outcome comparison period context method finding limitation "
    ).split()
    return " ".join(vocabulary[index % len(vocabulary)] for index in range(words))


def _pdf(pages: list[str]) -> bytes:
    # A tiny valid PDF generator keeps the recovery fixtures deterministic and
    # avoids committing binary test assets.
    page_numbers = [4 + index * 2 for index in range(len(pages))]
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (
            b"<< /Type /Pages /Kids ["
            + b" ".join(f"{number} 0 R".encode() for number in page_numbers)
            + f"] /Count {len(pages)} >>".encode()
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for index, text in enumerate(pages):
        page_number = 4 + index * 2
        content_number = page_number + 1
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 9 Tf 36 756 Td ({escaped}) Tj ET".encode("latin-1")
        objects.extend(
            [
                (
                    f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_number} 0 R >>"
                ).encode(),
                b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
            ]
        )
    content = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(content))
        content.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(content)
    content.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    content.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        content.extend(f"{offset:010d} 00000 n \n".encode())
    content.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(content)
