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


SourceScope = Literal[
    "full_document", "partial_document", "abstract_only", "metadata_only"
]
CoverageGate = Literal["passed", "limited", "failed"]


class ContentAdequacyClass(str, Enum):
    FULL_PDF_TEXT = "full_pdf_text"
    PARTIAL_PDF_TEXT = "partial_pdf_text"
    FULL_TEXT_DOCUMENT = "full_text_document"
    FULL_ARTICLE_HTML = "full_article_html"
    CLEAN_FULL_ARTICLE_HTML = "full_article_html"
    PARTIAL_ARTICLE_HTML = "partial_article_html"
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
        self.paragraph_count = 0
        self.article_paragraph_count = 0
        self.heading_count = 0
        self.article_heading_count = 0

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
        if tag == "p":
            self.paragraph_count += 1
            if self.article_depth:
                self.article_paragraph_count += 1
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.heading_count += 1
            if self.article_depth:
                self.article_heading_count += 1
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
    page_matches = re.findall(
        r"--- Page (\d+) ---\s*(.*?)(?=--- Page \d+ ---|\Z)",
        cleaned,
        flags=re.DOTALL,
    )
    marker_numbers = [int(number) for number, _section in page_matches]
    page_sections = [section for _number, section in page_matches]
    content_text = _clean_text("\n\n".join(page_sections)) if page_sections else cleaned
    coverage_metadata = coverage_metadata or {}
    metrics = _coverage_metrics(
        content_text,
        page_count=page_count,
        coverage_metadata=coverage_metadata,
    )
    nonempty_page_count = sum(len(_clean_text(section)) >= 20 for section in page_sections)
    covered_page_ratio = len(set(marker_numbers)) / page_count if page_count > 0 and page_sections else None
    nonempty_page_ratio = nonempty_page_count / page_count if page_count > 0 and page_sections else None
    markers_in_order = marker_numbers == list(range(1, page_count + 1)) if page_count > 0 else bool(page_sections)
    metrics.update(
        {
            "extracted_page_marker_count": len(page_sections),
            "nonempty_page_count": nonempty_page_count,
            "nonempty_page_ratio": nonempty_page_ratio,
            "covered_page_ratio": covered_page_ratio,
            "page_markers_in_order": markers_in_order,
        }
    )
    unresolved_pages = tuple(
        sorted(
            {
                value
                for raw in coverage_metadata.get("unresolved_pages", ()) or ()
                if (value := _positive_int(raw)) is not None
            }
        )
    )
    page_routes = tuple(
        str(value) for value in coverage_metadata.get("page_routes", ()) or ()
    )
    if page_routes and len(page_routes) == page_count:
        recovered_pages = tuple(
            index
            for index, route in enumerate(page_routes, start=1)
            if route != "unresolved"
        )
        recovered_text_pages = tuple(
            index
            for index, route in enumerate(page_routes, start=1)
            if route not in {"unresolved", "nonprose_or_blank"}
        )
    else:
        recovered_pages = tuple(
            number for number in marker_numbers if number not in unresolved_pages
        )
        recovered_text_pages = tuple(
            int(number)
            for number, section in page_matches
            if int(number) not in unresolved_pages
            and len(_clean_text(section)) >= 20
        )
    recovered_page_ratio = (
        len(set(recovered_pages)) / page_count if page_count > 0 else None
    )
    printed_page_map = _printed_page_map(
        page_count,
        coverage_metadata.get("ordinal_to_printed_page"),
    )
    spans = _document_spans(page_matches, printed_page_map)
    bibliography = _bibliography_only_analysis(content_text)
    metrics.update(
        {
            "unresolved_pages": unresolved_pages,
            "recovered_pages": recovered_pages,
            "recovered_text_pages": recovered_text_pages,
            "recovered_page_ratio": recovered_page_ratio,
            "ordinal_to_printed_page": printed_page_map,
            "heading_spans": spans["heading_spans"],
            "table_spans": spans["table_spans"],
            "figure_spans": spans["figure_spans"],
            **bibliography,
        }
    )
    for key in (
        "embedded_text_page_count",
        "ocr_page_count",
        "extraction_route",
        "page_routes",
        "orientation_retry_pages",
        "repeated_boilerplate_ratio",
    ):
        if key in coverage_metadata:
            metrics[key] = coverage_metadata[key]
    # A blank cover or divider still has full page coverage. Coverage therefore
    # follows the ordered markers, while substantive-text checks happen at the
    # document level.
    page_coverage_passed = markers_in_order
    # Page coverage says whether the extractor touched the attachment; it does
    # not establish that those pages contain publication text. Publisher error
    # pages and download-notice PDFs can contain a few repeated words on every
    # page and otherwise look "complete". Require a conservative amount of
    # usable prose for multi-page documents so those files proceed to the
    # existing OCR/fallback route instead of becoming analytical sources.
    minimum_word_count = max(200, 40 * page_count)
    metrics["minimum_word_count"] = minimum_word_count
    metrics["word_density_passed"] = metrics["word_count"] >= minimum_word_count
    if bibliography["bibliography_only"]:
        metrics["content_kind"] = "bibliography_only"
        return ContentAdequacy(
            classification=ContentAdequacyClass.METADATA_ONLY,
            source_scope="metadata_only",
            coverage_gate="failed",
            reason="bibliography_only_attachment",
            metrics=metrics,
        )
    metrics["content_kind"] = "document_text"
    if (
        metrics["char_count"] >= 80
        and metrics["word_density_passed"]
        and page_coverage_passed
        and not unresolved_pages
    ):
        return ContentAdequacy(
            classification=ContentAdequacyClass.FULL_PDF_TEXT,
            source_scope="full_document",
            coverage_gate="passed",
            reason="pdf_text_extracted",
            metrics=metrics,
        )
    if (
        metrics["char_count"] >= 80
        and metrics["word_density_passed"]
        and page_coverage_passed
        and unresolved_pages
        and recovered_page_ratio is not None
        and recovered_page_ratio >= 0.8
        and len(set(recovered_text_pages)) >= 2
    ):
        return ContentAdequacy(
            classification=ContentAdequacyClass.PARTIAL_PDF_TEXT,
            source_scope="partial_document",
            coverage_gate="limited",
            reason="partial_pdf_text_extracted",
            metrics=metrics,
        )
    return ContentAdequacy(
        classification=ContentAdequacyClass.METADATA_ONLY,
        source_scope="metadata_only",
        coverage_gate="failed",
        reason=(
            "insufficient_pdf_text_density"
            if page_coverage_passed and not metrics["word_density_passed"]
            else "insufficient_or_partial_pdf_text"
        ),
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
    paragraph_count = parser.paragraph_count
    visible_block_count = sum(
        len(re.findall(r"\b\w+\b", line, flags=re.UNICODE)) >= 5
        for line in visible.splitlines()
        if line.strip()
    )
    structured_block_count = paragraph_count + parser.heading_count
    strong_article_body = (
        parser.has_article_container
        and article_word_count >= 300
        and parser.article_paragraph_count >= 2
    )
    strong_visible_body = (
        metrics["word_count"] >= 500
        and max(structured_block_count, visible_block_count) >= 4
    )
    full_article_evidence = strong_article_body or strong_visible_body
    abstract_word_count = len(re.findall(r"\b\w+\b", abstract, flags=re.UNICODE))
    expected_page_match = re.search(
        r"\((\d{1,4})\s+pages?\)", visible, flags=re.IGNORECASE
    )
    expected_page_count = (
        int(expected_page_match.group(1)) if expected_page_match else 0
    )
    visible_folded = visible.casefold()
    viewer_start = visible_folded.find("this is the content viewer section")
    viewer_end = (
        visible_folded.find("explore jstor", viewer_start)
        if viewer_start >= 0
        else -1
    )
    viewer_text = (
        visible[viewer_start:viewer_end]
        if viewer_start >= 0 and viewer_end > viewer_start
        else ""
    )
    viewer_word_count = len(
        re.findall(r"\b\w+\b", viewer_text, flags=re.UNICODE)
    )
    jstor_body_word_count = article_word_count or viewer_word_count
    partial_jstor_viewer = bool(
        "jstor" in marker_text
        and expected_page_count >= 4
        and jstor_body_word_count >= 500
        and jstor_body_word_count < expected_page_count * 250
    )
    abstract_dominates = bool(
        abstract
        and (
            not article_word_count
            or article_word_count <= max(120, int(abstract_word_count * 1.5))
        )
    )
    enclosing_paywall = bool(paywall_markers) and (
        not full_article_evidence or abstract_dominates
    )
    metrics.update(
        {
            "article_char_count": article_char_count,
            "article_word_count": article_word_count,
            "paragraph_count": paragraph_count,
            "article_paragraph_count": parser.article_paragraph_count,
            "heading_count": parser.heading_count,
            "article_heading_count": parser.article_heading_count,
            "visible_block_count": visible_block_count,
            "article_section_count": section_count,
            "has_article_container": parser.has_article_container,
            "strong_article_body": strong_article_body,
            "strong_visible_body": strong_visible_body,
            "enclosing_paywall": enclosing_paywall,
            "abstract_char_count": len(abstract),
            "paywall_marker_count": len(paywall_markers),
            "access_marker_count": len(access_markers),
            "expected_page_count": expected_page_count,
            "viewer_word_count": viewer_word_count,
            "partial_jstor_viewer": partial_jstor_viewer,
        }
    )
    if partial_jstor_viewer:
        return ContentAdequacy(
            classification=ContentAdequacyClass.PARTIAL_ARTICLE_HTML,
            source_scope="partial_document",
            coverage_gate="limited",
            reason="partial_article_viewer_html",
            abstract=abstract,
            paywall_markers=paywall_markers,
            access_markers=access_markers,
            metrics=metrics,
        )
    if full_article_evidence and not enclosing_paywall:
        return ContentAdequacy(
            classification=ContentAdequacyClass.FULL_ARTICLE_HTML,
            source_scope="full_document",
            coverage_gate="passed",
            reason="clean_full_article_html",
            abstract=abstract,
            paywall_markers=paywall_markers,
            access_markers=access_markers,
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


def extract_path(
    path: Path,
    *,
    ocr_mode: Literal["auto", "off", "required"] = "auto",
    ocr_languages: tuple[str, ...] = ("eng",),
) -> ExtractionResult:
    if not path.exists() or not path.is_file():
        return ExtractionResult(status="failed", route="local_file", reason="file_not_found")
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    try:
        data = path.read_bytes()
    except OSError as exc:
        return ExtractionResult(status="failed", route="local_file", reason=f"read_error:{exc}", media_type=media_type)
    return extract_bytes(
        data,
        media_type=media_type,
        filename=path.name,
        ocr_mode=ocr_mode,
        ocr_languages=ocr_languages,
    )


def extract_bytes(
    data: bytes,
    *,
    media_type: str,
    filename: str = "",
    ocr_mode: Literal["auto", "off", "required"] = "auto",
    ocr_languages: tuple[str, ...] = ("eng",),
) -> ExtractionResult:
    if ocr_mode not in {"auto", "off", "required"}:
        raise ValueError("ocr_mode must be one of: auto, off, required")
    suffix = Path(filename).suffix.lower()
    if media_type == "application/pdf" or suffix == ".pdf":
        return _extract_pdf(data, ocr_mode=ocr_mode, ocr_languages=ocr_languages)
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


@dataclass(frozen=True, slots=True)
class _OCRPageResult:
    text: str = ""
    route: str = ""
    available: bool = False
    retry_used: bool = False
    nonprose_or_blank: bool = False


_BOILERPLATE_TERMS = (
    "downloaded from",
    "download this article",
    "article download",
    "access provided by",
    "access denied",
    "copyright",
    "all rights reserved",
    "watermark",
    "sage publications",
)


def _extract_pdf(
    data: bytes,
    *,
    ocr_mode: Literal["auto", "off", "required"] = "auto",
    ocr_languages: tuple[str, ...] = ("eng",),
) -> ExtractionResult:
    try:
        from pypdf import PdfReader
    except ImportError:
        return ExtractionResult(status="failed", route="pypdf_text", reason="pypdf_not_installed", media_type="application/pdf")
    try:
        reader = PdfReader(io.BytesIO(data))
        page_count = len(reader.pages)
        if page_count <= 0:
            return ExtractionResult(
                status="failed",
                route="pypdf_text",
                reason="pdf_page_count_unavailable",
                media_type="application/pdf",
            )
        embedded_pages = [_clean_text(page.extract_text() or "") for page in reader.pages]
        try:
            page_labels = tuple(str(value) for value in reader.page_labels)
        except (AttributeError, TypeError, ValueError):
            page_labels = ()
    except Exception as exc:
        return ExtractionResult(
            status="failed",
            route="pypdf_text",
            reason=f"pdf_error:{type(exc).__name__}",
            media_type="application/pdf",
        )
    repeated = _repeated_pdf_units(embedded_pages)
    suspicious_pages = {
        index
        for index, page_text in enumerate(embedded_pages)
        if _page_text_is_suspicious(page_text, repeated_units=repeated)
    }
    embedded_analysis = _document_text_analysis(embedded_pages)
    if embedded_analysis["document_suspicious"]:
        # The document-level test catches publisher-error PDFs such as Touval,
        # where every page has enough characters to evade a blank-page test.
        suspicious_pages.update(range(page_count))

    final_pages = list(embedded_pages)
    page_routes = ["embedded_text" if index not in suspicious_pages else "unresolved" for index in range(page_count)]
    ocr_pages: list[int] = []
    unresolved_pages: list[int] = []
    ocr_unavailable = False
    orientation_retries: list[int] = []
    if suspicious_pages and ocr_mode != "off":
        for index in sorted(suspicious_pages):
            recovered = _ocr_pdf_page(data, index, ocr_languages)
            if not recovered.available:
                ocr_unavailable = True
                unresolved_pages.append(index + 1)
                page_routes[index] = "unresolved"
                continue
            if recovered.retry_used:
                orientation_retries.append(index + 1)
            if not _page_text_is_suspicious(
                recovered.text
            ) or _short_ocr_text_is_readable(recovered.text):
                final_pages[index] = recovered.text
                page_routes[index] = recovered.route or "ocr"
                ocr_pages.append(index + 1)
            elif not recovered.nonprose_or_blank:
                unresolved_pages.append(index + 1)
                page_routes[index] = "unresolved"
            else:
                # No lexical OCR output is consistent with a blank cover,
                # divider, or illustration page. Such pages do not make an
                # otherwise substantive document incomplete.
                page_routes[index] = "nonprose_or_blank"
    elif suspicious_pages:
        for index in sorted(suspicious_pages):
            if _pdf_page_is_nonprose(data, index):
                page_routes[index] = "nonprose_or_blank"
            else:
                page_routes[index] = "unresolved"
                unresolved_pages.append(index + 1)

    text = _page_marked_text(final_pages)
    final_analysis = _document_text_analysis(final_pages)
    extraction_route = (
        "pypdf_pdfium_tesseract"
        if any(route.startswith("pdfium") for route in page_routes)
        else "pypdf_poppler_tesseract"
        if any(route.startswith("poppler") for route in page_routes)
        else "pypdf_text"
    )
    extra_metrics = {
        "embedded_text_page_count": sum(route == "embedded_text" for route in page_routes),
        "ocr_page_count": len(ocr_pages),
        "unresolved_pages": tuple(unresolved_pages),
        "extraction_route": extraction_route,
        "page_routes": tuple(page_routes),
        "orientation_retry_pages": tuple(orientation_retries),
        "repeated_boilerplate_ratio": embedded_analysis["dominant_repeated_ratio"],
        "ordinal_to_printed_page": {
            str(index): label
            for index, label in enumerate(page_labels, start=1)
            if label
        },
    }
    adequacy = classify_pdf_text(
        text,
        page_count=page_count,
        coverage_metadata=extra_metrics,
    )
    required_ocr_missing = ocr_mode == "required" and bool(suspicious_pages) and ocr_unavailable
    failed = (
        adequacy.coverage_gate == "failed"
        or final_analysis["document_suspicious"]
        or required_ocr_missing
    )
    if failed:
        return ExtractionResult(
            status="failed",
            text=text,
            route=extraction_route,
            reason=(
                "required_ocr_unavailable"
                if required_ocr_missing
                else "bibliography_only_attachment"
                if adequacy.reason == "bibliography_only_attachment"
                else "unresolved_textual_pages"
                if unresolved_pages
                else "empty_or_scanned_pdf"
            ),
            media_type="application/pdf",
            page_count=page_count,
            adequacy=adequacy,
        )
    return ExtractionResult(
        status="succeeded",
        text=text,
        route=extraction_route,
        reason=adequacy.reason if adequacy.source_scope == "partial_document" else "",
        media_type="application/pdf",
        page_count=page_count,
        adequacy=adequacy,
    )


def ocr_pdf_bytes(data: bytes) -> ExtractionResult:
    """Backward-compatible entry point for callers that explicitly request OCR."""

    result = _extract_pdf(data, ocr_mode="required")
    result.route = "local_ocr"
    return result


def _ocr_pdf_page(data: bytes, page_index: int, languages: tuple[str, ...]) -> _OCRPageResult:
    try:
        with tempfile.TemporaryDirectory(prefix="auto-zettelkasten-ocr-") as temporary:
            temporary_root = Path(temporary)
            rendered = _render_pdf_page(data, page_index, temporary_root)
            if rendered is None:
                return _OCRPageResult()
            image_path, renderer = rendered
            tesseract = shutil.which("tesseract")
            if not tesseract:
                return _OCRPageResult()
            language = "+".join(language.strip() for language in languages if language.strip()) or "eng"
            first = _run_tesseract(tesseract, image_path, language=language, psm=3)
            orientation_retry = _rendered_page_is_landscape(image_path)
            if not _page_text_is_suspicious(first) and not orientation_retry:
                return _OCRPageResult(
                    text=first,
                    route=f"{renderer}_tesseract",
                    available=True,
                )
            # PSM 1 is the single orientation-aware retry for a failed or
            # implausible first pass.
            second = _run_tesseract(tesseract, image_path, language=language, psm=1)
            chosen = second if not _page_text_is_suspicious(second) else first
            return _OCRPageResult(
                text=chosen,
                route=f"{renderer}_tesseract",
                available=True,
                retry_used=True,
                nonprose_or_blank=(
                    not _alphabetic_words(first) and not _alphabetic_words(second)
                    and _rendered_page_is_nonprose(
                        image_path, allow_sparse_divider=True
                    )
                ),
            )
    except (OSError, subprocess.SubprocessError, RuntimeError):
        return _OCRPageResult()


def _render_pdf_page(data: bytes, page_index: int, temporary_root: Path) -> tuple[Path, str] | None:
    try:
        import pypdfium2 as pdfium

        document = pdfium.PdfDocument(data)
        try:
            page = document[page_index]
            try:
                bitmap = page.render(scale=300 / 72)
                try:
                    image_path = temporary_root / f"page-{page_index + 1}.png"
                    bitmap.to_pil().save(image_path, format="PNG")
                finally:
                    bitmap.close()
            finally:
                page.close()
        finally:
            document.close()
        return image_path, "pdfium"
    except (ImportError, OSError, RuntimeError, ValueError):
        pass

    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm:
        return None
    source_path = temporary_root / "source.pdf"
    source_path.write_bytes(data)
    output_root = temporary_root / f"page-{page_index + 1}"
    completed = subprocess.run(
        [
            pdftoppm,
            "-f",
            str(page_index + 1),
            "-l",
            str(page_index + 1),
            "-singlefile",
            "-r",
            "300",
            "-png",
            str(source_path),
            str(output_root),
        ],
        check=False,
        capture_output=True,
        timeout=120,
    )
    image_path = output_root.with_suffix(".png")
    return (image_path, "poppler") if completed.returncode == 0 and image_path.exists() else None


def _run_tesseract(tesseract: str, image_path: Path, *, language: str, psm: int) -> str:
    completed = subprocess.run(
        [tesseract, str(image_path), "stdout", "-l", language, "--psm", str(psm)],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return _clean_text(completed.stdout) if completed.returncode == 0 else ""


def _rendered_page_is_landscape(image_path: Path) -> bool:
    """Flag the common pixel-rotated scan shape for orientation-aware OCR."""

    try:
        from PIL import Image

        with Image.open(image_path) as image:
            width, height = image.size
    except (OSError, ValueError):
        return False
    return width > height * 1.08


def _rendered_page_is_nonprose(
    image_path: Path, *, allow_sparse_divider: bool = False
) -> bool:
    """Recognize nonprose only after OCR has failed to find lexical text.

    Sparse layout alone is not enough: three short prose lines can resemble a
    divider in pixel-density statistics.  Callers may admit the sparse-divider
    shape only after both OCR passes returned no alphabetic words.
    """

    try:
        from PIL import Image

        with Image.open(image_path) as image:
            gray = image.convert("L")
            gray.thumbnail((900, 900))
            histogram = gray.histogram()
            total = max(1, sum(histogram))
            dark_ratio = sum(histogram[:210]) / total
            pixels = gray.load()
            width, height = gray.size
            occupied_rows = [
                row
                for row in range(height)
                if sum(1 for column in range(width) if pixels[column, row] < 180)
                >= max(2, int(width * 0.001))
            ]
    except (OSError, ValueError):
        return False
    row_bands = 0
    previous = -10
    for row in occupied_rows:
        if row - previous > 3:
            row_bands += 1
        previous = row
    sparse_divider = dark_ratio < 0.03 and row_bands <= 4
    return (
        dark_ratio < 0.002
        or dark_ratio > 0.35
        or (allow_sparse_divider and sparse_divider)
    )


def _pdf_page_is_nonprose(data: bytes, page_index: int) -> bool:
    """Inspect a suspicious page visually without invoking OCR."""

    try:
        with tempfile.TemporaryDirectory(prefix="auto-zettelkasten-sniff-") as temporary:
            rendered = _render_pdf_page(data, page_index, Path(temporary))
            return bool(rendered and _rendered_page_is_nonprose(rendered[0]))
    except (OSError, RuntimeError):
        return False


def _page_marked_text(pages: list[str]) -> str:
    return _clean_text(
        "\n\n".join(f"--- Page {number} ---\n{text}" for number, text in enumerate(pages, start=1))
    )


def _page_text_is_suspicious(text: str, *, repeated_units: set[str] | None = None) -> bool:
    alphanumeric_count = sum(character.isalnum() for character in text)
    words = _alphabetic_words(text)
    if alphanumeric_count < 40 or len(words) < 6:
        return True
    normalized = text.casefold()
    if repeated_units and any(term in normalized for term in _BOILERPLATE_TERMS):
        page_units = _normalized_pdf_units(text)
        total_tokens = max(1, len(words))
        repeated_tokens = sum(len(unit.split()) for unit in page_units if unit in repeated_units)
        if repeated_tokens / total_tokens >= 0.5:
            return True
    return False


def _short_ocr_text_is_readable(text: str) -> bool:
    """Accept a legible short title or divider after OCR without weakening routing."""

    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if not normalized or any(
        term in normalized.casefold() for term in _BOILERPLATE_TERMS
    ):
        return False
    words = _alphabetic_words(normalized)
    alphanumeric_count = sum(character.isalnum() for character in normalized)
    return bool(words and (len(words) >= 2 or alphanumeric_count >= 8))


def _alphabetic_words(text: str) -> list[str]:
    return re.findall(
        r"[^\W\d_]+(?:['’][^\W\d_]+)*",
        text,
        flags=re.UNICODE,
    )


def _normalized_pdf_units(text: str) -> tuple[str, ...]:
    units: list[str] = []
    for raw in re.split(r"\n+", text):
        normalized = re.sub(r"\s+", " ", raw.casefold()).strip()
        normalized = re.sub(r"\b\d+\b", "#", normalized)
        if len(_alphabetic_words(normalized)) >= 3:
            units.append(normalized)
    return tuple(units)


def _repeated_pdf_units(pages: list[str]) -> set[str]:
    page_occurrences: dict[str, int] = {}
    for page in pages:
        for unit in set(_normalized_pdf_units(page)):
            page_occurrences[unit] = page_occurrences.get(unit, 0) + 1
    return {unit for unit, count in page_occurrences.items() if count >= 3}


def _document_text_analysis(pages: list[str]) -> dict[str, Any]:
    repeated = _repeated_pdf_units(pages)
    all_words = [word.casefold() for page in pages for word in _alphabetic_words(page)]
    total_tokens = max(1, len(all_words))
    unit_occurrences: dict[str, int] = {}
    for page in pages:
        for unit in _normalized_pdf_units(page):
            if unit in repeated:
                unit_occurrences[unit] = unit_occurrences.get(unit, 0) + 1
    dominant_repeated_ratio = max(
        (len(unit.split()) * count / total_tokens for unit, count in unit_occurrences.items()),
        default=0.0,
    )
    stripped_words = 0
    lexical_paragraphs = 0
    for page in pages:
        for raw in re.split(r"\n\s*\n|\n", page):
            normalized = re.sub(r"\s+", " ", raw.casefold()).strip()
            normalized_numberless = re.sub(r"\b\d+\b", "#", normalized)
            words = _alphabetic_words(raw)
            if normalized_numberless not in repeated or len(words) > 30:
                stripped_words += len(words)
            if len(words) >= 6 and len({word.casefold() for word in words}) >= 4:
                lexical_paragraphs += 1
    minimum_words = max(200, 40 * len(pages))
    document_suspicious = (
        stripped_words < minimum_words
        or dominant_repeated_ratio >= 0.5
        or lexical_paragraphs == 0
    )
    return {
        "minimum_word_count": minimum_words,
        "stripped_word_count": stripped_words,
        "dominant_repeated_ratio": round(dominant_repeated_ratio, 6),
        "lexical_paragraph_count": lexical_paragraphs,
        "document_suspicious": document_suspicious,
    }


def _adequacy_with_metrics(adequacy: ContentAdequacy, extra: Mapping[str, Any]) -> ContentAdequacy:
    metrics = dict(adequacy.metrics or {})
    metrics.update(extra)
    return ContentAdequacy(
        classification=adequacy.classification,
        source_scope=adequacy.source_scope,
        coverage_gate=adequacy.coverage_gate,
        reason=adequacy.reason,
        abstract=adequacy.abstract,
        paywall_markers=adequacy.paywall_markers,
        access_markers=adequacy.access_markers,
        metrics=metrics,
    )


def _positive_int(value: Any) -> int | None:
    try:
        converted = int(value)
    except (TypeError, ValueError):
        return None
    return converted if converted > 0 else None


def _printed_page_map(page_count: int, value: Any) -> dict[str, str]:
    supplied = value if isinstance(value, Mapping) else {}
    return {
        str(index): str(supplied.get(str(index)) or supplied.get(index) or index)
        for index in range(1, page_count + 1)
    }


def _document_spans(
    page_matches: list[tuple[str, str]],
    printed_page_map: Mapping[str, str],
) -> dict[str, tuple[dict[str, Any], ...]]:
    headings: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    figures: list[dict[str, Any]] = []
    known_heading = re.compile(
        r"^(?:abstract|introduction|background|literature review|methods?|"
        r"methodology|data|results?|findings?|discussion|conclusions?|"
        r"limitations?|references|bibliography|works cited|appendix)\b",
        flags=re.IGNORECASE,
    )
    numbered_heading = re.compile(
        r"^(?:\d+(?:\.\d+){0,3}|[IVXLCDM]+)\.?\s+[A-Z][^\n]{2,120}$"
    )
    object_heading = re.compile(
        r"^(?P<kind>table|figure)\s+(?P<label>[A-Z0-9]+(?:\.[A-Z0-9]+)*)"
        r"(?:\s*[:.-]\s*|\s+).{0,140}$",
        flags=re.IGNORECASE,
    )
    for raw_number, section in page_matches:
        page = int(raw_number)
        for raw_line in section.splitlines():
            line = re.sub(r"\s+", " ", raw_line).strip()
            if not line or len(line) > 180:
                continue
            span = {
                "label": line,
                "page_ordinal": page,
                "printed_page": str(printed_page_map.get(str(page)) or page),
            }
            object_match = object_heading.match(line)
            if object_match:
                target = (
                    tables
                    if object_match.group("kind").casefold() == "table"
                    else figures
                )
                target.append(span)
            elif known_heading.match(line) or numbered_heading.match(line):
                headings.append(span)
            if len(headings) + len(tables) + len(figures) >= 512:
                break
    return {
        "heading_spans": tuple(headings),
        "table_spans": tuple(tables),
        "figure_spans": tuple(figures),
    }


def _bibliography_only_analysis(text: str) -> dict[str, Any]:
    headings = list(
        re.finditer(
            r"(?im)^\s*(?:references|bibliography|works cited)\s*$",
            text,
        )
    )
    heading = headings[-1] if headings else None
    total_words = len(_alphabetic_words(text))
    if not heading or total_words < 800:
        return {
            "bibliography_only": False,
            "bibliography_reference_count": 0,
            "bibliography_word_ratio": 0.0,
            "pre_bibliography_word_count": total_words,
        }
    before = text[: heading.start()]
    after = text[heading.end() :]
    before_words = len(_alphabetic_words(before))
    after_words = len(_alphabetic_words(after))
    cover_followup = after[:200_000]
    archive_body_heading = re.search(
        r"(?im)^\s*(?:abstract|introduction|methods?|results?|discussion|conclusions?)\s*$",
        cover_followup,
    ) or re.search(
        r"(?m)^\s*(?!(?:REFERENCES|BIBLIOGRAPHY|WORKS CITED)\s*$)"
        r"(?:[A-Z][A-Z0-9'’&:,-]*\s+){2,}[A-Z][A-Z0-9'’&:,-]*\s*$",
        cover_followup,
    )
    if (
        heading.start() <= int(len(text) * 0.08)
        and archive_body_heading
    ):
        return {
            "bibliography_only": False,
            "bibliography_reference_count": 0,
            "bibliography_word_ratio": 0.0,
            "pre_bibliography_word_count": before_words,
        }
    reference_count = sum(
        bool(
            re.search(r"\b(?:18|19|20)\d{2}[a-z]?\b", line)
            or re.search(r"\bdoi\s*:", line, flags=re.IGNORECASE)
            or re.search(r"https?://", line)
        )
        for line in after.splitlines()
        if line.strip()
    )
    bibliography_word_ratio = after_words / max(1, total_words)
    bibliography_only = (
        reference_count >= 20
        and bibliography_word_ratio >= 0.75
        and before_words <= max(400, int(total_words * 0.08))
    )
    return {
        "bibliography_only": bibliography_only,
        "bibliography_reference_count": reference_count,
        "bibliography_word_ratio": round(bibliography_word_ratio, 6),
        "pre_bibliography_word_count": before_words,
    }


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
