from __future__ import annotations

import html
import io
import mimetypes
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path


@dataclass(slots=True)
class ExtractionResult:
    status: str
    text: str = ""
    route: str = ""
    reason: str = ""
    media_type: str = ""
    page_count: int = 0


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript"}:
            self.hidden_depth += 1
        if tag.lower() in {"p", "br", "div", "section", "article", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript"} and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            self.parts.append(data)


def extract_path(path: Path) -> ExtractionResult:
    if not path.exists() or not path.is_file():
        return ExtractionResult(status="failed", route="local_file", reason="file_not_found")
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    try:
        data = path.read_bytes()
    except OSError as exc:
        return ExtractionResult(status="failed", route="local_file", reason=f"read_error:{exc}", media_type=media_type)
    return extract_bytes(data, media_type=media_type, filename=path.name)


def extract_bytes(data: bytes, *, media_type: str, filename: str = "") -> ExtractionResult:
    suffix = Path(filename).suffix.lower()
    if media_type == "application/pdf" or suffix == ".pdf":
        return _extract_pdf(data)
    if media_type in {"text/html", "application/xhtml+xml"} or suffix in {".html", ".htm"}:
        decoded = _decode_text(data)
        parser = _HTMLTextExtractor()
        parser.feed(decoded)
        text = _clean_text(html.unescape(" ".join(parser.parts)))
        return _text_result(text, "html_text", media_type or "text/html")
    if media_type.startswith("text/") or suffix in {".txt", ".md", ".rst", ".csv"}:
        text = _clean_text(_decode_text(data))
        return _text_result(text, "plain_text", media_type or "text/plain")
    return ExtractionResult(status="failed", route="unsupported", reason="unsupported_media_type", media_type=media_type)


def _extract_pdf(data: bytes) -> ExtractionResult:
    try:
        from pypdf import PdfReader
    except ImportError:
        return ExtractionResult(status="failed", route="pypdf_text", reason="pypdf_not_installed", media_type="application/pdf")
    try:
        reader = PdfReader(io.BytesIO(data))
        page_text = [f"--- Page {number} ---\n{page.extract_text() or ''}" for number, page in enumerate(reader.pages, start=1)]
        text = _clean_text("\n\n".join(page_text))
    except Exception as exc:
        return ExtractionResult(
            status="failed",
            route="pypdf_text",
            reason=f"pdf_error:{type(exc).__name__}",
            media_type="application/pdf",
        )
    if len(text) < 80:
        return ExtractionResult(
            status="failed",
            text=text,
            route="pypdf_text",
            reason="empty_or_scanned_pdf",
            media_type="application/pdf",
            page_count=len(reader.pages),
        )
    return ExtractionResult(
        status="succeeded",
        text=text,
        route="pypdf_text",
        media_type="application/pdf",
        page_count=len(reader.pages),
    )


def ocr_pdf_bytes(data: bytes) -> ExtractionResult:
    tesseract = shutil.which("tesseract")
    if not tesseract:
        return ExtractionResult(status="failed", route="local_ocr", reason="tesseract_not_installed", media_type="application/pdf")
    try:
        from pypdf import PdfReader
    except ImportError:
        return ExtractionResult(status="failed", route="local_ocr", reason="pypdf_not_installed", media_type="application/pdf")
    try:
        reader = PdfReader(io.BytesIO(data))
        outputs: list[str] = []
        image_count = 0
        with tempfile.TemporaryDirectory(prefix="auto-zettelkasten-ocr-") as temporary:
            temporary_root = Path(temporary)
            for page_number, page in enumerate(reader.pages, start=1):
                page_outputs: list[str] = []
                for image_number, image in enumerate(page.images, start=1):
                    image_count += 1
                    suffix = Path(str(image.name or "image.png")).suffix or ".png"
                    image_path = temporary_root / f"page-{page_number}-{image_number}{suffix}"
                    image_path.write_bytes(image.data)
                    completed = subprocess.run(
                        [tesseract, str(image_path), "stdout"],
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=120,
                    )
                    if completed.returncode == 0 and completed.stdout.strip():
                        page_outputs.append(completed.stdout.strip())
                if page_outputs:
                    outputs.append(f"--- Page {page_number} ---\n" + "\n".join(page_outputs))
    except (OSError, subprocess.SubprocessError, Exception) as exc:
        return ExtractionResult(
            status="failed",
            route="local_ocr",
            reason=f"ocr_error:{type(exc).__name__}",
            media_type="application/pdf",
        )
    text = _clean_text("\n\n".join(outputs))
    if image_count == 0:
        return ExtractionResult(status="failed", route="local_ocr", reason="no_extractable_images", media_type="application/pdf", page_count=len(reader.pages))
    if len(text) < 40:
        return ExtractionResult(status="failed", text=text, route="local_ocr", reason="insufficient_ocr_text", media_type="application/pdf", page_count=len(reader.pages))
    return ExtractionResult(status="succeeded", text=text, route="local_ocr", media_type="application/pdf", page_count=len(reader.pages))


def _text_result(text: str, route: str, media_type: str) -> ExtractionResult:
    if len(text) < 40:
        return ExtractionResult(status="failed", text=text, route=route, reason="insufficient_text", media_type=media_type)
    return ExtractionResult(status="succeeded", text=text, route=route, media_type=media_type)


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "utf-16", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _clean_text(value: str) -> str:
    value = value.replace("\x00", " ")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n\s*\n\s*\n+", "\n\n", value)
    return value.strip()
