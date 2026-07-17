from __future__ import annotations

import html
import io
import mimetypes
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from enum import Enum
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal, Mapping


SourceScope = Literal["full_document", "abstract_only", "metadata_only"]
CoverageGate = Literal["passed", "limited", "failed"]


class ContentAdequacyClass(str, Enum):
    FULL_PDF_TEXT = "full_pdf_text"
    FULL_TEXT_DOCUMENT = "full_text_document"
    FULL_ARTICLE_HTML = "full_article_html"
    CLEAN_FULL_ARTICLE_HTML = "full_article_html"
    ABSTRACT_PAYWALL_HTML = "abstract_paywall_html"
    ABSTRACT_OR_PAYWALL_HTML = "abstract_paywall_html"
    METADATA_ONLY = "metadata_only"


ContentAdequacyKind = ContentAdequacyClass


PAYWALL_MARKERS = (
    "you do not currently have access to this article",
    "purchase access",
    "subscribe to read",
    "subscription required",
    "rent this article",
    "buy this article",
    "full text unavailable",
    "paywall",
    "purchase",
    "rental",
)

ACCESS_MARKERS = (
    "sign in via your institution",
    "access through your institution",
    "institutional access",
    "sign in to access",
    "log in to access",
    "check access",
    "get access",
)

_ARTICLE_SECTION_MARKERS = ("introduction", "methods", "methodology", "results", "discussion", "conclusion")
_ABSTRACT_META_NAMES = ("citation_abstract", "dc.description", "dcterms.abstract", "prism.abstract")
_DESCRIPTION_META_NAMES = ("description", "og:description", "twitter:description")


@dataclass(frozen=True, slots=True)
class ContentAdequacy:
    classification: ContentAdequacyClass
    source_scope: SourceScope
    coverage_gate: CoverageGate
    reason: str
    abstract: str = ""
    paywall_markers: tuple[str, ...] = ()
    access_markers: tuple[str, ...] = ()
    metrics: Mapping[str, Any] | None = None

    @property
    def is_full_publication(self) -> bool:
        return self.source_scope == "full_document" and self.coverage_gate == "passed"

    @property
    def gate(self) -> CoverageGate:
        return self.coverage_gate

    def to_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification.value,
            "source_scope": self.source_scope,
            "coverage_gate": self.coverage_gate,
            "reason": self.reason,
            "abstract": self.abstract,
            "paywall_markers": list(self.paywall_markers),
            "access_markers": list(self.access_markers),
            "metrics": dict(self.metrics or {}),
        }


@dataclass(slots=True)
class ExtractionResult:
    status: str
    text: str = ""
    route: str = ""
    reason: str = ""
    media_type: str = ""
    page_count: int = 0
    adequacy: ContentAdequacy | None = None

    @property
    def source_scope(self) -> str:
        return self.adequacy.source_scope if self.adequacy else ""

    @property
    def source_coverage(self) -> str:
        return self.adequacy.coverage_gate if self.adequacy else ""

    @property
    def coverage_metrics(self) -> dict[str, Any]:
        return dict(self.adequacy.metrics or {}) if self.adequacy else {}


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.article_parts: list[str] = []
        self.abstract_parts: list[str] = []
        self.meta: dict[str, str] = {}
        self.hidden_depth = 0
        self.article_depth = 0
        self._abstract_containers: list[str] = []
        self.has_article_container = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attributes = {str(key).casefold(): str(value or "") for key, value in attrs}
        if tag in {"script", "style", "noscript"}:
            self.hidden_depth += 1
        if tag == "meta":
            name = (attributes.get("name") or attributes.get("property") or "").casefold().strip()
            content = attributes.get("content", "").strip()
            if name and content and name not in self.meta:
                self.meta[name] = content
        if tag in {"article", "main"}:
            self.article_depth += 1
            self.has_article_container = True
        identity = f"{attributes.get('id', '')} {attributes.get('class', '')}".casefold()
        if "abstract" in identity:
            self._abstract_containers.append(tag)
        if tag in {"p", "br", "div", "section", "article", "main", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")
            if self.article_depth:
                self.article_parts.append("\n")
            if self._abstract_containers:
                self.abstract_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript"} and self.hidden_depth:
            self.hidden_depth -= 1
        if self._abstract_containers and tag == self._abstract_containers[-1]:
            self._abstract_containers.pop()
        if tag in {"article", "main"} and self.article_depth:
            self.article_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            self.parts.append(data)
            if self.article_depth:
                self.article_parts.append(data)
            if self._abstract_containers:
                self.abstract_parts.append(data)


def classify_pdf_text(
    text: str,
    *,
    page_count: int = 0,
    coverage_metadata: Mapping[str, Any] | None = None,
) -> ContentAdequacy:
    cleaned = _clean_text(text)
    page_sections = re.findall(r"--- Page \d+ ---\s*(.*?)(?=--- Page \d+ ---|\Z)", cleaned, flags=re.DOTALL)
    content_text = _clean_text("\n\n".join(page_sections)) if page_sections else cleaned
    metrics = _coverage_metrics(content_text, page_count=page_count, coverage_metadata=coverage_metadata)
    nonempty_page_count = sum(len(_clean_text(section)) >= 20 for section in page_sections)
    covered_page_ratio = nonempty_page_count / page_count if page_count > 0 and page_sections else None
    metrics.update(
        {
            "extracted_page_marker_count": len(page_sections),
            "nonempty_page_count": nonempty_page_count,
            "covered_page_ratio": covered_page_ratio,
        }
    )
    page_coverage_passed = covered_page_ratio is None or covered_page_ratio >= 0.8
    if metrics["char_count"] >= 80 and metrics["word_count"] >= 8 and page_coverage_passed:
        return ContentAdequacy(
            classification=ContentAdequacyClass.FULL_PDF_TEXT,
            source_scope="full_document",
            coverage_gate="passed",
            reason="pdf_text_extracted",
            metrics=metrics,
        )
    return ContentAdequacy(
        classification=ContentAdequacyClass.METADATA_ONLY,
        source_scope="metadata_only",
        coverage_gate="failed",
        reason="insufficient_or_partial_pdf_text",
        metrics=metrics,
    )


def extract_abstract_from_html(raw_html: str) -> str:
    parser = _parse_html(raw_html)
    for name in _ABSTRACT_META_NAMES:
        value = parser.meta.get(name)
        if value:
            return _clean_text(html.unescape(value))
    container_text = _clean_text(html.unescape(" ".join(parser.abstract_parts)))
    container_text = re.sub(r"^abstract\s*[:.-]?\s*", "", container_text, flags=re.IGNORECASE)
    if container_text:
        return container_text
    visible = _clean_text(html.unescape(" ".join(parser.parts)))
    match = re.search(
        r"(?:^|\n)\s*abstract\s*[:.-]?\s*(.*?)"
        r"(?=\n\s*(?:(?:keywords?|introduction|methods?|background)\b|©|"
        r"you do not currently have access to this article\b|sign in via your institution\b|purchase\b|rental\b)|\Z)",
        visible,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match and match.group(1).strip():
        return _clean_text(match.group(1))
    for name in _DESCRIPTION_META_NAMES:
        value = parser.meta.get(name)
        if value and len(_clean_text(value)) >= 40:
            return _clean_text(html.unescape(value))
    return ""


def classify_html_content(
    raw_html: str,
    *,
    coverage_metadata: Mapping[str, Any] | None = None,
) -> ContentAdequacy:
    parser = _parse_html(raw_html)
    visible = _clean_text(html.unescape(" ".join(parser.parts)))
    article_text = _clean_text(html.unescape(" ".join(parser.article_parts)))
    abstract = extract_abstract_from_html(raw_html)
    marker_text = f"{visible}\n{raw_html}".casefold()
    paywall_markers = _matched_markers(marker_text, PAYWALL_MARKERS)
    access_markers = _matched_markers(marker_text, ACCESS_MARKERS)
    metrics = _coverage_metrics(visible, coverage_metadata=coverage_metadata)
    section_count = sum(bool(re.search(rf"\b{re.escape(marker)}\b", article_text, flags=re.IGNORECASE)) for marker in _ARTICLE_SECTION_MARKERS)
    article_char_count = len(article_text)
    article_word_count = len(re.findall(r"\b\w+\b", article_text, flags=re.UNICODE))
    paragraph_count = len(re.findall(r"<p\b", raw_html, flags=re.IGNORECASE))
    full_article_evidence = parser.has_article_container and article_word_count >= 1_500 and section_count >= 2
    metrics.update(
        {
            "article_char_count": article_char_count,
            "article_word_count": article_word_count,
            "paragraph_count": paragraph_count,
            "article_section_count": section_count,
            "has_article_container": parser.has_article_container,
            "abstract_char_count": len(abstract),
            "paywall_marker_count": len(paywall_markers),
            "access_marker_count": len(access_markers),
        }
    )
    if full_article_evidence and not paywall_markers and not (abstract and access_markers):
        return ContentAdequacy(
            classification=ContentAdequacyClass.FULL_ARTICLE_HTML,
            source_scope="full_document",
            coverage_gate="passed",
            reason="clean_full_article_html",
            abstract=abstract,
            metrics=metrics,
        )
    if abstract or paywall_markers or access_markers:
        return ContentAdequacy(
            classification=ContentAdequacyClass.ABSTRACT_PAYWALL_HTML,
            source_scope="abstract_only" if abstract else "metadata_only",
            coverage_gate="limited",
            reason="paywall_or_abstract_only_html",
            abstract=abstract,
            paywall_markers=paywall_markers,
            access_markers=access_markers,
            metrics=metrics,
        )
    return ContentAdequacy(
        classification=ContentAdequacyClass.METADATA_ONLY,
        source_scope="metadata_only",
        coverage_gate="failed",
        reason="no_full_article_or_abstract_evidence",
        metrics=metrics,
    )


def classify_metadata_only(metadata: Mapping[str, Any] | None = None) -> ContentAdequacy:
    metadata = metadata or {}
    present_fields = tuple(sorted(str(key) for key, value in metadata.items() if value not in (None, "", [], {})))
    metrics = _coverage_metrics("", coverage_metadata=metadata)
    metrics.update({"metadata_field_count": len(present_fields), "metadata_fields": present_fields})
    return ContentAdequacy(
        classification=ContentAdequacyClass.METADATA_ONLY,
        source_scope="metadata_only",
        coverage_gate="failed",
        reason="metadata_only",
        metrics=metrics,
    )


def classify_plain_text(text: str, *, coverage_metadata: Mapping[str, Any] | None = None) -> ContentAdequacy:
    cleaned = _clean_text(text)
    metrics = _coverage_metrics(cleaned, coverage_metadata=coverage_metadata)
    if metrics["char_count"] >= 80 and metrics["word_count"] >= 8:
        return ContentAdequacy(
            classification=ContentAdequacyClass.FULL_TEXT_DOCUMENT,
            source_scope="full_document",
            coverage_gate="passed",
            reason="plain_text_document",
            metrics=metrics,
        )
    return classify_metadata_only(coverage_metadata)


def classify_content_adequacy(
    content: str = "",
    *,
    media_type: str = "",
    raw_html: str | None = None,
    page_count: int = 0,
    coverage_metadata: Mapping[str, Any] | None = None,
) -> ContentAdequacy:
    normalized_media_type = media_type.casefold().split(";", 1)[0].strip()
    if normalized_media_type == "application/pdf":
        return classify_pdf_text(content, page_count=page_count, coverage_metadata=coverage_metadata)
    if raw_html is not None or normalized_media_type in {"text/html", "application/xhtml+xml"}:
        return classify_html_content(raw_html if raw_html is not None else content, coverage_metadata=coverage_metadata)
    marker_text = content.casefold()
    if any(marker in marker_text for marker in PAYWALL_MARKERS + ACCESS_MARKERS):
        return classify_html_content(content, coverage_metadata=coverage_metadata)
    has_abstract_heading = bool(re.search(r"(?:^|\n)\s*abstract\s*[:.-]?", content, flags=re.IGNORECASE))
    if has_abstract_heading:
        word_count = len(re.findall(r"\b\w+\b", content, flags=re.UNICODE))
        section_count = sum(
            bool(re.search(rf"(?:^|\n)\s*{re.escape(marker)}\s*[:.-]?\s*(?:\n|$)", content, flags=re.IGNORECASE))
            for marker in _ARTICLE_SECTION_MARKERS
        )
        if word_count < 1_500 or section_count < 2:
            return classify_html_content(content, coverage_metadata=coverage_metadata)
    if normalized_media_type.startswith("text/"):
        return classify_plain_text(content, coverage_metadata=coverage_metadata)
    return classify_metadata_only(coverage_metadata)


# Backward-friendly concise alias for callers that use the media-specific name.
classify_html = classify_html_content


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
        parser = _parse_html(decoded)
        text = _clean_text(html.unescape(" ".join(parser.parts)))
        return _text_result(
            text,
            "html_text",
            media_type or "text/html",
            adequacy=classify_html_content(decoded),
        )
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
    adequacy = classify_pdf_text(text, page_count=len(reader.pages))
    if not adequacy.is_full_publication:
        return ExtractionResult(
            status="failed",
            text=text,
            route="pypdf_text",
            reason="empty_or_scanned_pdf",
            media_type="application/pdf",
            page_count=len(reader.pages),
            adequacy=adequacy,
        )
    return ExtractionResult(
        status="succeeded",
        text=text,
        route="pypdf_text",
        media_type="application/pdf",
        page_count=len(reader.pages),
        adequacy=adequacy,
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
        return ExtractionResult(
            status="failed",
            route="local_ocr",
            reason="no_extractable_images",
            media_type="application/pdf",
            page_count=len(reader.pages),
            adequacy=classify_pdf_text("", page_count=len(reader.pages)),
        )
    if len(text) < 40:
        return ExtractionResult(
            status="failed",
            text=text,
            route="local_ocr",
            reason="insufficient_ocr_text",
            media_type="application/pdf",
            page_count=len(reader.pages),
            adequacy=classify_pdf_text(text, page_count=len(reader.pages)),
        )
    return ExtractionResult(
        status="succeeded",
        text=text,
        route="local_ocr",
        media_type="application/pdf",
        page_count=len(reader.pages),
        adequacy=classify_pdf_text(text, page_count=len(reader.pages)),
    )


def _text_result(
    text: str,
    route: str,
    media_type: str,
    *,
    adequacy: ContentAdequacy | None = None,
) -> ExtractionResult:
    if len(text) < 40:
        return ExtractionResult(
            status="failed",
            text=text,
            route=route,
            reason="insufficient_text",
            media_type=media_type,
            adequacy=adequacy,
        )
    return ExtractionResult(status="succeeded", text=text, route=route, media_type=media_type, adequacy=adequacy)


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "utf-16", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _parse_html(raw_html: str) -> _HTMLTextExtractor:
    parser = _HTMLTextExtractor()
    parser.feed(raw_html)
    parser.close()
    return parser


def _coverage_metrics(
    text: str,
    *,
    page_count: int = 0,
    coverage_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = coverage_metadata or {}
    indexed_chars = _nonnegative_int(metadata.get("indexedChars", metadata.get("indexed_chars")))
    total_chars = _nonnegative_int(metadata.get("totalChars", metadata.get("total_chars")))
    indexed_pages = _nonnegative_int(metadata.get("indexedPages", metadata.get("indexed_pages")))
    total_pages = _nonnegative_int(metadata.get("totalPages", metadata.get("total_pages")))
    return {
        "char_count": len(text),
        "word_count": len(re.findall(r"\b\w+\b", text, flags=re.UNICODE)),
        "line_count": len(text.splitlines()) if text else 0,
        "page_count": page_count,
        "indexed_chars": indexed_chars,
        "total_chars": total_chars,
        "indexed_pages": indexed_pages,
        "total_pages": total_pages,
        "indexed_chars_reported_complete": bool(total_chars and indexed_chars is not None and indexed_chars >= total_chars),
        "indexed_pages_reported_complete": bool(total_pages and indexed_pages is not None and indexed_pages >= total_pages),
    }


def _nonnegative_int(value: Any) -> int | None:
    try:
        converted = int(value)
    except (TypeError, ValueError):
        return None
    return converted if converted >= 0 else None


def _matched_markers(text: str, markers: tuple[str, ...]) -> tuple[str, ...]:
    found = [marker for marker in markers if marker in text]
    return tuple(marker for marker in found if not any(marker != other and marker in other for other in found))


def _clean_text(value: str) -> str:
    value = value.replace("\x00", " ")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n\s*\n\s*\n+", "\n\n", value)
    return value.strip()
