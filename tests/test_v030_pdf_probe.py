from __future__ import annotations

import hashlib
import io
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import auto_zettelkasten.extraction as extraction


def test_pdf_probe_collects_no_ocr_page_and_custody_evidence(monkeypatch) -> None:
    def unexpected(*_args, **_kwargs):
        raise AssertionError("the structural probe must not OCR or render")

    monkeypatch.setattr(extraction, "_ocr_pdf_page", unexpected)
    monkeypatch.setattr(extraction, "_render_pdf_page", unexpected)
    data = _pdf([_prose("embedded", 220), "scan"])

    probe = extraction.probe_pdf_bytes(data)

    assert probe.status == "succeeded"
    assert probe.custody_sha256 == hashlib.sha256(data).hexdigest()
    assert probe.custody_byte_count == len(data)
    assert probe.page_count == 2
    assert probe.suspicious_pages == (2,)
    assert probe.render_candidate_pages == (2,)
    assert probe.pages[0].embedded_text.startswith("embedded evidence")
    assert probe.pages[0].text_quality == "good"
    assert probe.pages[0].resource_types == ("Font",)
    assert probe.pages[0].resource_count == 1
    assert probe.pages[0].image_count == 0
    assert probe.pages[0].width == 1_583
    assert probe.pages[0].height == 2_048
    assert probe.pages[1].suspicious is True
    assert probe.pages[1].render_candidate is True
    assert probe.pages[1].printed_page == ""
    assert probe.adequacy is not None
    assert probe.to_dict()["pages"][1]["embedded_text"] == "scan"


def test_pdf_probe_retains_other_pages_after_one_page_text_error(
    monkeypatch,
) -> None:
    class FakePage:
        mediabox = SimpleNamespace(width=72, height=144)

        def __init__(self, text: str, *, broken: bool = False) -> None:
            self.text = text
            self.broken = broken

        def get(self, key: str, default=None):  # noqa: ANN001
            if key == "/Resources":
                return {
                    "/Font": {"/F1": {}},
                    "/XObject": {
                        "/Im1": {"/Subtype": "/Image"},
                        "/Fm1": {"/Subtype": "/Form"},
                    },
                }
            return default

        def extract_text(self) -> str:
            if self.broken:
                raise ValueError("private parser detail must not escape")
            return self.text

    class FakeReader:
        is_encrypted = False
        root_object = {"/PageLabels": {}}
        page_labels = ("i", "ii")

        def __init__(self, _stream) -> None:  # noqa: ANN001
            self.pages = [
                FakePage(_prose("retained", 220)),
                FakePage("", broken=True),
            ]

    import pypdf

    monkeypatch.setattr(pypdf, "PdfReader", FakeReader)
    probe = extraction.probe_pdf_bytes(b"synthetic custody bytes")

    assert probe.status == "partial"
    assert probe.reason == "partial_page_text"
    assert probe.page_errors == ((2, "ValueError"),)
    assert probe.pages[0].embedded_text.startswith("retained evidence")
    assert probe.pages[0].image_count == 1
    assert probe.pages[0].xobject_count == 2
    assert probe.pages[0].visually_consequential is True
    assert probe.pages[0].render_candidate is True
    assert (probe.pages[0].width, probe.pages[0].height) == (300, 600)
    assert probe.pages[1].text_quality == "corrupted"
    assert probe.pages[1].error_type == "ValueError"
    assert probe.pages[1].printed_page == "ii"


@pytest.mark.parametrize("label", [None, "empty", "empty_nums", 1, 42])
def test_pdf_probe_does_not_invent_printed_labels(label: int | str | None) -> None:
    from pypdf import PdfWriter
    from pypdf.generic import ArrayObject, DictionaryObject, NameObject

    writer = PdfWriter(clone_from=io.BytesIO(_pdf([_prose("Introduction", 220)])))
    if isinstance(label, int):
        writer.set_page_label(0, 0, style="/D", start=label)
    elif label is not None:
        writer.root_object[NameObject("/PageLabels")] = DictionaryObject(
            {NameObject("/Nums"): ArrayObject()} if label == "empty_nums" else {}
        )
    stream = io.BytesIO()
    writer.write(stream)
    data = stream.getvalue()

    probe = extraction.probe_pdf_bytes(data)
    result = extraction.extract_pdf_from_probe(data, probe, ocr_mode="off")
    expected = {"1": str(label)} if isinstance(label, int) and label != 1 else {}

    assert result.status == "succeeded"
    assert probe.pages[0].page_number == 1
    assert probe.pages[0].printed_page == expected.get("1", "")
    assert probe.adequacy.metrics["ordinal_to_printed_page"] == expected
    assert result.coverage_metrics["ordinal_to_printed_page"] == expected


def test_extract_pdf_from_probe_reuses_custody_checked_evidence(monkeypatch) -> None:
    data = _pdf([_prose("reuse", 220)])
    probe = extraction.probe_pdf_bytes(data)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("the completed structural probe must be reused")

    monkeypatch.setattr(extraction, "probe_pdf_bytes", unexpected)
    monkeypatch.setattr(extraction, "_ocr_pdf_page", unexpected)
    result = extraction.extract_pdf_from_probe(data, probe)

    assert result.status == "succeeded"
    assert result.route == "pypdf_text"
    assert "reuse evidence" in result.text
    with pytest.raises(ValueError, match="custody does not match"):
        extraction.extract_pdf_from_probe(data + b"changed", probe)


def test_extract_pdf_from_probe_ocrs_pages_with_text_parser_errors(
    monkeypatch,
) -> None:
    data = _pdf([_prose("retained", 220), "scan"])
    probe = extraction.probe_pdf_bytes(data)
    broken = replace(
        probe.pages[1],
        embedded_text="",
        embedded_text_sha256=hashlib.sha256(b"").hexdigest(),
        embedded_char_count=0,
        embedded_word_count=0,
        text_quality="corrupted",
        suspicious=True,
        render_candidate=True,
        error_type="ValueError",
    )
    probe = replace(
        probe,
        status="partial",
        reason="partial_page_text",
        pages=(probe.pages[0], broken),
        suspicious_pages=(2,),
        render_candidate_pages=(2,),
    )
    calls: list[int] = []

    def recover(_data: bytes, page_index: int, _languages: tuple[str, ...]):
        calls.append(page_index)
        return extraction._OCRPageResult(
            text=_prose("recovered", 80),
            route="pdfium_tesseract",
            available=True,
        )

    monkeypatch.setattr(extraction, "_ocr_pdf_page", recover)

    off = extraction.extract_pdf_from_probe(data, probe, ocr_mode="off")
    result = extraction.extract_pdf_from_probe(data, probe, ocr_mode="auto")
    required = extraction.extract_pdf_from_probe(data, probe, ocr_mode="required")

    assert off.status == "failed"
    assert off.reason == "pdf_error:ValueError"
    assert result.status == "succeeded"
    assert required.status == "succeeded"
    assert result.route == "pypdf_pdfium_tesseract"
    assert calls == [1, 1]
    assert "retained evidence" in result.text
    assert "recovered evidence" in result.text

    monkeypatch.setattr(
        extraction,
        "_ocr_pdf_page",
        lambda *_args, **_kwargs: extraction._OCRPageResult(),
    )
    unavailable = extraction.extract_pdf_from_probe(
        data,
        probe,
        ocr_mode="required",
    )
    assert unavailable.status == "failed"
    assert unavailable.reason == "required_ocr_unavailable"


def test_pdf_page_rendering_is_ordered_bounded_and_deterministic(
    tmp_path: Path,
) -> None:
    data = _pdf([_prose("one", 40), _prose("two", 40), _prose("three", 40)])

    first = extraction.render_pdf_pages(data, [3, 1], tmp_path / "first")
    second = extraction.render_pdf_pages(data, [1, 3], tmp_path / "second")

    assert [page.page_number for page in first] == [1, 3]
    assert [page.path.name for page in first] == ["page-0001.png", "page-0003.png"]
    assert [(page.width, page.height) for page in first] == [
        (page.width, page.height) for page in second
    ]
    assert [page.sha256 for page in first] == [page.sha256 for page in second]
    assert all(max(page.width, page.height) == 2_048 for page in first)
    assert all(page.media_type == "image/png" for page in first)
    assert first[0].to_dict()["path"] == str(first[0].path)


def test_pdf_page_rendering_never_enlarges_and_rejects_invalid_selection(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from PIL import Image

    def fake_render(
        _data: bytes,
        page_index: int,
        temporary_root: Path,
        *,
        cancelled=None,  # noqa: ANN001
    ):
        assert page_index == 0
        assert cancelled is None
        path = temporary_root / "raw.png"
        Image.new("RGBA", (40, 20), color=(12, 34, 56, 128)).save(path)
        return path, "fake"

    monkeypatch.setattr(extraction, "_render_pdf_page", fake_render)
    rendered = extraction.render_pdf_pages(b"not-read", [1], tmp_path / "small")

    assert (rendered[0].width, rendered[0].height) == (40, 20)
    with Image.open(rendered[0].path) as image:
        assert image.mode == "RGB"
    with pytest.raises(ValueError, match="at most 16"):
        extraction.render_pdf_pages(b"not-read", range(1, 18), tmp_path / "many")
    with pytest.raises(ValueError, match="positive 1-based"):
        extraction.render_pdf_pages(b"not-read", [0], tmp_path / "zero")
    with pytest.raises(ValueError, match="unique"):
        extraction.render_pdf_pages(b"not-read", [1, 1], tmp_path / "duplicate")


def test_pdfium_renderer_is_serialized_across_threads(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from PIL import Image

    active = 0
    peak = 0
    state_lock = threading.Lock()

    class FakeBitmap:
        def to_pil(self):
            return Image.new("RGB", (10, 10), "white")

        def close(self) -> None:
            return None

    class FakePage:
        def render(self, *, scale: float):
            nonlocal active, peak
            assert scale == 300 / 72
            with state_lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.01)
            with state_lock:
                active -= 1
            return FakeBitmap()

        def close(self) -> None:
            return None

    class FakeDocument:
        def __init__(self, _data: bytes) -> None:
            pass

        def __getitem__(self, _page_index: int) -> FakePage:
            return FakePage()

        def close(self) -> None:
            return None

    monkeypatch.setitem(
        sys.modules,
        "pypdfium2",
        SimpleNamespace(PdfDocument=FakeDocument),
    )

    def render(index: int) -> str | None:
        output = tmp_path / str(index)
        output.mkdir()
        result = extraction._render_pdf_page(b"pdf", 0, output)
        return result[1] if result else None

    with ThreadPoolExecutor(max_workers=4) as executor:
        assert list(executor.map(render, range(4))) == ["pdfium"] * 4
    assert peak == 1


def test_pdfium_renderer_lock_wait_is_cancellable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class UnexpectedDocument:
        def __init__(self, _data: bytes) -> None:
            raise AssertionError("cancelled rendering must not reach PDFium")

    monkeypatch.setitem(
        sys.modules,
        "pypdfium2",
        SimpleNamespace(PdfDocument=UnexpectedDocument),
    )
    stop = threading.Event()
    waiting = threading.Event()
    checks = 0

    def cancelled() -> bool:
        nonlocal checks
        checks += 1
        if checks >= 2:
            waiting.set()
        return stop.is_set()

    extraction._PDFIUM_RENDER_LOCK.acquire()
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(
            extraction._render_pdf_page,
            b"pdf",
            0,
            tmp_path,
            cancelled=cancelled,
        )
        assert waiting.wait(timeout=1)
        stop.set()
        with pytest.raises(extraction.ExtractionCancelled):
            future.result(timeout=1)
    finally:
        extraction._PDFIUM_RENDER_LOCK.release()
        executor.shutdown(wait=True)


def _prose(label: str, words: int) -> str:
    vocabulary = (
        f"{label} evidence describes process actors outcome comparison period "
        "context method finding limitation "
    ).split()
    return " ".join(vocabulary[index % len(vocabulary)] for index in range(words))


def _pdf(pages: list[str]) -> bytes:
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
                    f"/Resources << /Font << /F1 3 0 R >> >> "
                    f"/Contents {content_number} 0 R >>"
                ).encode(),
                b"<< /Length "
                + str(len(stream)).encode()
                + b" >>\nstream\n"
                + stream
                + b"\nendstream",
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
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(content)
