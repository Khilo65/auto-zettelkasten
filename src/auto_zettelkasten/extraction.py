from __future__ import annotations

import hashlib
import html
import importlib.metadata
import io
import math
import mimetypes
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import Enum
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Sequence


SourceScope = Literal[
    "full_document", "partial_document", "abstract_only", "metadata_only"
]
CoverageGate = Literal["passed", "limited", "failed"]


class ExtractionCancelled(RuntimeError):
    pass


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
_HTML_VOID_ELEMENTS = frozenset(
    "area base br col embed hr img input link meta param source track wbr".split()
)
_HTML_TABLE_ELEMENTS = frozenset("table caption thead tbody tfoot tr th td".split())
# ponytail: global PDFium lock; use a renderer process if PDF-heavy throughput becomes limiting.
_PDFIUM_RENDER_LOCK = threading.Lock()


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


PDFPageTextQuality = Literal[
    "good",
    "partial",
    "image_only",
    "suspicious",
    "corrupted",
    "encrypted_or_unavailable",
]


@dataclass(frozen=True, slots=True)
class PDFPageEvidence:
    page_number: int
    printed_page: str
    width: int
    height: int
    embedded_text: str
    embedded_text_sha256: str
    embedded_char_count: int
    embedded_word_count: int
    text_quality: PDFPageTextQuality
    resource_types: tuple[str, ...]
    resource_count: int
    xobject_count: int
    image_count: int
    suspicious: bool
    visually_consequential: bool
    render_candidate: bool
    error_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_number": self.page_number,
            "printed_page": self.printed_page,
            "width": self.width,
            "height": self.height,
            "embedded_text": self.embedded_text,
            "embedded_text_sha256": self.embedded_text_sha256,
            "embedded_char_count": self.embedded_char_count,
            "embedded_word_count": self.embedded_word_count,
            "text_quality": self.text_quality,
            "resource_types": list(self.resource_types),
            "resource_count": self.resource_count,
            "xobject_count": self.xobject_count,
            "image_count": self.image_count,
            "suspicious": self.suspicious,
            "visually_consequential": self.visually_consequential,
            "render_candidate": self.render_candidate,
            "error_type": self.error_type,
        }


@dataclass(frozen=True, slots=True)
class PDFStructuralProbe:
    status: Literal["succeeded", "partial", "failed"]
    reason: str
    media_type: str
    custody_sha256: str
    custody_byte_count: int
    page_count: int
    pages: tuple[PDFPageEvidence, ...]
    embedded_text: str
    adequacy: ContentAdequacy | None
    document_analysis: Mapping[str, Any]
    suspicious_pages: tuple[int, ...]
    render_candidate_pages: tuple[int, ...]
    page_labels: tuple[str, ...]

    @property
    def embedded_pages(self) -> tuple[str, ...]:
        return tuple(page.embedded_text for page in self.pages)

    @property
    def page_errors(self) -> tuple[tuple[int, str], ...]:
        return tuple(
            (page.page_number, page.error_type)
            for page in self.pages
            if page.error_type
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "media_type": self.media_type,
            "custody_sha256": self.custody_sha256,
            "custody_byte_count": self.custody_byte_count,
            "page_count": self.page_count,
            "pages": [page.to_dict() for page in self.pages],
            "embedded_text": self.embedded_text,
            "adequacy": self.adequacy.to_dict() if self.adequacy else None,
            "document_analysis": dict(self.document_analysis),
            "suspicious_pages": list(self.suspicious_pages),
            "render_candidate_pages": list(self.render_candidate_pages),
            "page_labels": list(self.page_labels),
        }


@dataclass(frozen=True, slots=True)
class PDFPageImage:
    page_number: int
    path: Path
    media_type: str
    width: int
    height: int
    sha256: str
    byte_count: int
    renderer: str
    renderer_version: str
    render_policy_version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_number": self.page_number,
            "path": str(self.path),
            "media_type": self.media_type,
            "width": self.width,
            "height": self.height,
            "sha256": self.sha256,
            "byte_count": self.byte_count,
            "renderer": self.renderer,
            "renderer_version": self.renderer_version,
            "render_policy_version": self.render_policy_version,
        }


@dataclass(slots=True)
class _HTMLBody:
    parts: list[str] = field(default_factory=list)
    paragraphs: int = 0
    headings: int = 0
    primary: bool = False


class _HTMLTextExtractor(HTMLParser):
    def __init__(self, *, preserve_tables: bool = False) -> None:
        super().__init__()
        self.preserve_tables = preserve_tables
        self.parts: list[str] = []
        self.article_parts: list[str] = []
        self.abstract_parts: list[str] = []
        self.meta: dict[str, str] = {}
        self.hidden_depth = 0
        self._body_stack: list[tuple[str, _HTMLBody | None, _HTMLBody | None, bool]] = []
        self._articles: list[_HTMLBody] = []
        self._explicit_bodies: dict[int, _HTMLBody] = {}
        self.has_specific_body = False
        self._abstract_containers: list[str] = []
        self.has_article_container = False
        self.paragraph_count = 0
        self.article_paragraph_count = 0
        self.heading_count = 0
        self.article_heading_count = 0
        self.heading_spans: list[dict[str, str]] | None = []
        self._heading_tag = ""
        self._heading_parts: list[str] = []
        self._heading_hidden_tags: list[str] = []
        self.selected_option_parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attributes = {str(key).casefold(): str(value or "") for key, value in attrs}
        if (
            self._heading_hidden_tags or "hidden" in attributes
            or attributes.get("aria-hidden", "").strip().casefold() == "true"
        ) and tag not in _HTML_VOID_ELEMENTS:
            self._heading_hidden_tags.append(tag)
        if tag == "option":
            self._finish_selected_option()
        if tag in {"script", "style", "noscript"}:
            self.hidden_depth += 1
        if self.preserve_tables and not self.hidden_depth and tag in _HTML_TABLE_ELEMENTS:
            # Keep source grouping without expanding spans or guessing header/value alignment.
            spans = "".join(
                f' {key}="{attributes[key]}"'
                for key in ("rowspan", "colspan")
                if tag in {"th", "td"} and re.fullmatch(r"[0-9]{1,5}", attributes.get(key, ""))
            )
            self.parts.append(f"<{tag}{spans}>")
        if tag == "meta":
            name = (attributes.get("name") or attributes.get("property") or "").casefold().strip()
            content = attributes.get("content", "").strip()
            if name and content and name not in self.meta:
                self.meta[name] = content
        if tag == "option" and any(
            str(key).casefold() == "selected" for key, _value in attrs
        ):
            self.selected_option_parts = []
        article, body, excluded = self._body_stack[-1][1:] if self._body_stack else (None, None, False)
        identity = " ".join(
            value for key, value in attributes.items()
            if key in {"id", "class", "name", "itemprop", "role", "data-test", "data-testid", "data-qa", "data-module"}
        )
        identity = re.sub(r"([a-z])([A-Z])", r"\1-\2", identity).casefold()
        excluded = excluded or bool(self.hidden_depth) or tag in {"aside", "nav", "footer"} or bool(re.search(
            r"(?:^|[^a-z])(?:sub-?comments?|comments?|related|recommendations?|recommended)(?:$|[^a-z])",
            identity,
        ))
        if tag == "article" and not excluded:
            article = _HTMLBody(primary=bool(article and article.primary))
            self._articles.append(article)
        if not excluded and re.search(r"(?:^|[^a-z])article[-_ ]?body(?:$|[^a-z])", identity):
            # Sibling body fragments belong together only within the same article.
            if body is None:
                body = self._explicit_bodies.setdefault(id(article) if article else len(self._explicit_bodies), _HTMLBody())
        if tag not in _HTML_VOID_ELEMENTS:
            self._body_stack.append((tag, article, body, excluded))
        candidate = None if excluded else body or article
        if tag == "h1" and not excluded:
            for _tag, ancestor, _body, _excluded in self._body_stack:
                if ancestor:
                    ancestor.primary = True
        if tag == "p":
            self.paragraph_count += 1
            if candidate:
                candidate.paragraphs += 1
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.heading_count += 1
            if candidate:
                candidate.headings += 1
            if self._heading_tag:
                self.heading_spans = None
            if not self.hidden_depth and not self._heading_hidden_tags:
                self._heading_tag = tag
                self._heading_parts = []
        if tag == "br" and self._heading_tag and not self.hidden_depth and not self._heading_hidden_tags:
            self._heading_parts.append(" ")
        identity = f"{attributes.get('id', '')} {attributes.get('class', '')}".casefold()
        if "abstract" in identity:
            self._abstract_containers.append(tag)
        if tag in {"p", "br", "div", "section", "article", "main", "li", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n")
            if candidate:
                candidate.parts.append("\n")
            if self._abstract_containers:
                self.abstract_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.preserve_tables and not self.hidden_depth and tag in _HTML_TABLE_ELEMENTS:
            self.parts.append(f"</{tag}>")
        if self._heading_hidden_tags and tag not in _HTML_VOID_ELEMENTS:
            if tag == self._heading_hidden_tags[-1]:
                self._heading_hidden_tags.pop()
            else:
                self.heading_spans = None
        if (
            tag in {"h1", "h2", "h3", "h4", "h5", "h6"}
            and self._heading_tag and tag != self._heading_tag
        ):
            self.heading_spans = None
        if tag == self._heading_tag and not self.hidden_depth and not self._heading_hidden_tags:
            label = " ".join("".join(self._heading_parts).split())
            if self.heading_spans is not None and 0 < len(label) <= 180:
                if len(self.heading_spans) < 512:
                    self.heading_spans.append({"label": label})
                else:
                    # A truncated list cannot establish that a heading is unique.
                    self.heading_spans = None
            self._heading_tag = ""
            self._heading_parts = []
        if tag in {"option", "select"}:
            self._finish_selected_option()
        if tag in {"script", "style", "noscript"} and self.hidden_depth:
            self.hidden_depth -= 1
        if self._abstract_containers and tag == self._abstract_containers[-1]:
            self._abstract_containers.pop()
        for index in range(len(self._body_stack) - 1, -1, -1):
            if self._body_stack[index][0] == tag:
                del self._body_stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            self.parts.append(data)
            if self._heading_tag and not self._heading_hidden_tags:
                self._heading_parts.append(data)
            if self.selected_option_parts is not None:
                self.selected_option_parts.append(data)
            self._append_body_text(data)
            if self._abstract_containers:
                self.abstract_parts.append(data)

    def close(self) -> None:
        super().close()
        if self._heading_tag or self._heading_hidden_tags:
            self.heading_spans = None
        self._finish_selected_option()
        # ponytail: h1 identifies an unlabelled primary article; add structural
        # signals if a publisher omits both h1 and an explicit body identity.
        primary_articles = [article for article in self._articles if article.primary]
        candidates = list(self._explicit_bodies.values()) or primary_articles or self._articles
        self.has_specific_body = bool(self._explicit_bodies or primary_articles)
        self.has_article_container = bool(candidates)
        if candidates:
            candidate = max(candidates, key=lambda body: len(" ".join(body.parts)))
            self.article_parts = candidate.parts
            self.article_paragraph_count = candidate.paragraphs
            self.article_heading_count = candidate.headings

    def _append_body_text(self, text: str) -> None:
        if self._body_stack:
            _tag, article, body, excluded = self._body_stack[-1]
            if not excluded and (candidate := body or article):
                candidate.parts.append(text)

    def _finish_selected_option(self) -> None:
        if self.selected_option_parts is None:
            return
        label = " ".join(" ".join(self.selected_option_parts).split())
        if label:
            marker = f"\nSelected option: {label}\n"
            self.parts.append(marker)
            self._append_body_text(marker)
            if self._abstract_containers:
                self.abstract_parts.append(marker)
        self.selected_option_parts = None


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
        not parser.has_specific_body
        and metrics["word_count"] >= 500
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
    explicit_abstract = bool(
        any(parser.meta.get(name) for name in _ABSTRACT_META_NAMES)
        or parser.abstract_parts
        or re.search(
            r"(?:^|\n)\s*abstract\s*[:.-]?",
            visible,
            flags=re.IGNORECASE,
        )
    )
    body_word_count = (
        article_word_count
        if explicit_abstract
        else article_word_count or metrics["word_count"]
    )
    abstract_dominates = bool(
        abstract
        and body_word_count <= max(120, int(abstract_word_count * 1.5))
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
            "heading_spans": parser.heading_spans or [],
            "visible_block_count": visible_block_count,
            "article_section_count": section_count,
            "has_article_container": parser.has_article_container,
            "has_specific_body": parser.has_specific_body,
            "strong_article_body": strong_article_body,
            "strong_visible_body": strong_visible_body,
            "explicit_abstract": explicit_abstract,
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
    cancelled: Callable[[], bool] | None = None,
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
        cancelled=cancelled,
    )


def extract_bytes(
    data: bytes,
    *,
    media_type: str,
    filename: str = "",
    ocr_mode: Literal["auto", "off", "required"] = "auto",
    ocr_languages: tuple[str, ...] = ("eng",),
    cancelled: Callable[[], bool] | None = None,
) -> ExtractionResult:
    if ocr_mode not in {"auto", "off", "required"}:
        raise ValueError("ocr_mode must be one of: auto, off, required")
    suffix = Path(filename).suffix.lower()
    if media_type == "application/pdf" or suffix == ".pdf":
        return _extract_pdf(
            data,
            ocr_mode=ocr_mode,
            ocr_languages=ocr_languages,
            cancelled=cancelled,
        )
    if media_type in {"text/html", "application/xhtml+xml"} or suffix in {".html", ".htm"}:
        decoded = _decode_text(data)
        parser = _parse_html(decoded, preserve_tables=True)
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

PDF_PAGE_IMAGE_MAX_COUNT = 16
PDF_PAGE_IMAGE_MAX_SIDE = 2_048
PDF_PAGE_IMAGE_RENDER_POLICY_VERSION = "1"


def _pdf_object(value: Any) -> Any:
    getter = getattr(value, "get_object", None)
    return getter() if callable(getter) else value


def _pdf_page_resource_evidence(
    page: Any,
) -> tuple[tuple[str, ...], int, int, int]:
    """Inspect page resources without decoding images or rasterizing the page."""

    try:
        resources = _pdf_object(page.get("/Resources"))
        if not isinstance(resources, Mapping):
            return (), 0, 0, 0
        resource_types = tuple(
            sorted(str(key).removeprefix("/") for key in resources)
        )
        xobjects = _pdf_object(resources.get("/XObject"))
        if not isinstance(xobjects, Mapping):
            return resource_types, len(resources), 0, 0
        image_count = 0
        for value in xobjects.values():
            candidate = _pdf_object(value)
            if isinstance(candidate, Mapping) and str(candidate.get("/Subtype")) == "/Image":
                image_count += 1
        return resource_types, len(resources), len(xobjects), image_count
    except Exception:
        return (), 0, 0, 0


def _pdf_page_projected_dimensions(page: Any) -> tuple[int, int]:
    """Project the canonical 300-DPI render size without rasterizing the page."""

    try:
        raw_user_unit = abs(float(page.get("/UserUnit", 1) or 1))
        user_unit = raw_user_unit if raw_user_unit > 0 else 1.0
        width_points = abs(float(page.mediabox.width)) * user_unit
        height_points = abs(float(page.mediabox.height)) * user_unit
        rotation = int(page.get("/Rotate", 0) or 0) % 360
        if rotation in {90, 270}:
            width_points, height_points = height_points, width_points
        width = max(1, math.ceil(width_points * 300 / 72))
        height = max(1, math.ceil(height_points * 300 / 72))
        scale = min(1.0, PDF_PAGE_IMAGE_MAX_SIDE / max(width, height))
        return (
            max(1, min(PDF_PAGE_IMAGE_MAX_SIDE, math.ceil(width * scale))),
            max(1, min(PDF_PAGE_IMAGE_MAX_SIDE, math.ceil(height * scale))),
        )
    except (AttributeError, TypeError, ValueError, OverflowError):
        return PDF_PAGE_IMAGE_MAX_SIDE, PDF_PAGE_IMAGE_MAX_SIDE


def _pdf_visual_span_pages(adequacy: ContentAdequacy) -> set[int]:
    metrics = adequacy.metrics or {}
    pages: set[int] = set()
    for key in ("table_spans", "figure_spans"):
        spans = metrics.get(key, ())
        if not isinstance(spans, (list, tuple)):
            continue
        for span in spans:
            if not isinstance(span, Mapping):
                continue
            page_number = _positive_int(span.get("page_ordinal"))
            if page_number is not None:
                pages.add(page_number)
    return pages


def probe_pdf_bytes(
    data: bytes,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> PDFStructuralProbe:
    """Collect embedded-text and structural PDF evidence without OCR or rendering."""

    custody_sha256 = hashlib.sha256(data).hexdigest()
    failure = {
        "status": "failed",
        "media_type": "application/pdf",
        "custody_sha256": custody_sha256,
        "custody_byte_count": len(data),
        "page_count": 0,
        "pages": (),
        "embedded_text": "",
        "adequacy": None,
        "document_analysis": {},
        "suspicious_pages": (),
        "render_candidate_pages": (),
        "page_labels": (),
    }
    try:
        from pypdf import PdfReader
    except ImportError:
        return PDFStructuralProbe(reason="pypdf_not_installed", **failure)

    _raise_if_cancelled(cancelled)
    try:
        reader = PdfReader(io.BytesIO(data))
        page_count = len(reader.pages)
        if page_count <= 0:
            return PDFStructuralProbe(
                reason="pdf_page_count_unavailable",
                **failure,
            )
        try:
            # pypdf also synthesizes ordinals for malformed label trees.
            # Matching labels cannot disambiguate printed pagination.
            page_labels = (
                tuple(
                    str(value) if str(value) != str(index) else ""
                    for index, value in enumerate(reader.page_labels, start=1)
                )
                if "/PageLabels" in reader.root_object
                else ()
            )
        except (AttributeError, TypeError, ValueError):
            page_labels = ()
    except ExtractionCancelled:
        raise
    except Exception as exc:
        return PDFStructuralProbe(
            reason=f"pdf_error:{type(exc).__name__}",
            **failure,
        )

    embedded_pages: list[str] = []
    page_errors: list[str] = []
    resource_evidence: list[tuple[tuple[str, ...], int, int, int]] = []
    projected_dimensions: list[tuple[int, int]] = []
    encrypted = bool(getattr(reader, "is_encrypted", False))
    for index in range(page_count):
        _raise_if_cancelled(cancelled)
        try:
            page = reader.pages[index]
        except Exception as exc:
            embedded_pages.append("")
            page_errors.append(type(exc).__name__)
            resource_evidence.append(((), 0, 0, 0))
            projected_dimensions.append(
                (PDF_PAGE_IMAGE_MAX_SIDE, PDF_PAGE_IMAGE_MAX_SIDE)
            )
            continue
        resource_evidence.append(_pdf_page_resource_evidence(page))
        projected_dimensions.append(_pdf_page_projected_dimensions(page))
        try:
            embedded_pages.append(_pdf_embedded_text(page))
            page_errors.append("")
        except ExtractionCancelled:
            raise
        except Exception as exc:
            embedded_pages.append("")
            page_errors.append(type(exc).__name__)

    repeated = _repeated_pdf_units(embedded_pages)
    locally_suspicious = {
        index
        for index, page_text in enumerate(embedded_pages)
        if page_errors[index]
        or _page_text_is_suspicious(page_text, repeated_units=repeated)
    }
    document_analysis = _document_text_analysis(embedded_pages)
    suspicious = set(locally_suspicious)
    if document_analysis["document_suspicious"]:
        suspicious.update(range(page_count))

    printed_pages = tuple(
        page_labels[index] if index < len(page_labels) else ""
        for index in range(page_count)
    )
    embedded_text = _page_marked_text(embedded_pages)
    coverage_metadata = {
        "embedded_text_page_count": page_count - len(suspicious),
        "ocr_page_count": 0,
        "unresolved_pages": tuple(index + 1 for index in sorted(suspicious)),
        "extraction_route": "pypdf_text",
        "page_routes": tuple(
            "unresolved" if index in suspicious else "embedded_text"
            for index in range(page_count)
        ),
        "orientation_retry_pages": (),
        "repeated_boilerplate_ratio": document_analysis[
            "dominant_repeated_ratio"
        ],
        "ordinal_to_printed_page": {
            str(index): label
            for index, label in enumerate(printed_pages, start=1)
            if label
        },
    }
    adequacy = classify_pdf_text(
        embedded_text,
        page_count=page_count,
        coverage_metadata=coverage_metadata,
    )
    visual_span_pages = _pdf_visual_span_pages(adequacy)

    pages: list[PDFPageEvidence] = []
    for index, text in enumerate(embedded_pages):
        page_number = index + 1
        width, height = projected_dimensions[index]
        resource_types, resource_count, xobject_count, image_count = resource_evidence[index]
        error_type = page_errors[index]
        if error_type:
            text_quality: PDFPageTextQuality = (
                "encrypted_or_unavailable" if encrypted else "corrupted"
            )
        elif not text and image_count:
            text_quality = "image_only"
        elif index in locally_suspicious:
            text_quality = "suspicious"
        elif index in suspicious:
            text_quality = "partial"
        else:
            text_quality = "good"
        visually_consequential = bool(
            xobject_count or page_number in visual_span_pages
        )
        render_candidate = index in suspicious or visually_consequential
        pages.append(
            PDFPageEvidence(
                page_number=page_number,
                printed_page=printed_pages[index],
                width=width,
                height=height,
                embedded_text=text,
                embedded_text_sha256=hashlib.sha256(
                    text.encode("utf-8")
                ).hexdigest(),
                embedded_char_count=len(text),
                embedded_word_count=len(_alphabetic_words(text)),
                text_quality=text_quality,
                resource_types=resource_types,
                resource_count=resource_count,
                xobject_count=xobject_count,
                image_count=image_count,
                suspicious=index in suspicious,
                visually_consequential=visually_consequential,
                render_candidate=render_candidate,
                error_type=error_type,
            )
        )

    return PDFStructuralProbe(
        status="partial" if any(page_errors) else "succeeded",
        reason="partial_page_text" if any(page_errors) else "",
        media_type="application/pdf",
        custody_sha256=custody_sha256,
        custody_byte_count=len(data),
        page_count=page_count,
        pages=tuple(pages),
        embedded_text=embedded_text,
        adequacy=adequacy,
        document_analysis=document_analysis,
        suspicious_pages=tuple(index + 1 for index in sorted(suspicious)),
        render_candidate_pages=tuple(
            page.page_number for page in pages if page.render_candidate
        ),
        page_labels=page_labels,
    )


def _extract_pdf(
    data: bytes,
    *,
    ocr_mode: Literal["auto", "off", "required"] = "auto",
    ocr_languages: tuple[str, ...] = ("eng",),
    cancelled: Callable[[], bool] | None = None,
) -> ExtractionResult:
    probe = probe_pdf_bytes(data, cancelled=cancelled)
    return extract_pdf_from_probe(
        data,
        probe,
        ocr_mode=ocr_mode,
        ocr_languages=ocr_languages,
        cancelled=cancelled,
    )


def extract_pdf_from_probe(
    data: bytes,
    probe: PDFStructuralProbe,
    *,
    ocr_mode: Literal["auto", "off", "required"] = "auto",
    ocr_languages: tuple[str, ...] = ("eng",),
    cancelled: Callable[[], bool] | None = None,
) -> ExtractionResult:
    """Continue the existing PDF/OCR route from verified structural evidence."""

    if ocr_mode not in {"auto", "off", "required"}:
        raise ValueError("ocr_mode must be one of: auto, off, required")
    if (
        probe.custody_byte_count != len(data)
        or probe.custody_sha256 != hashlib.sha256(data).hexdigest()
    ):
        raise ValueError("PDF probe custody does not match source bytes")
    if probe.status == "failed":
        return ExtractionResult(
            status="failed",
            route="pypdf_text",
            reason=probe.reason,
            media_type="application/pdf",
        )
    if probe.page_errors and ocr_mode == "off":
        return ExtractionResult(
            status="failed",
            route="pypdf_text",
            reason=f"pdf_error:{probe.page_errors[0][1]}",
            media_type="application/pdf",
        )
    page_count = probe.page_count
    embedded_pages = list(probe.embedded_pages)
    page_labels = probe.page_labels
    suspicious_pages = {
        page_number - 1 for page_number in probe.suspicious_pages
    }
    embedded_analysis = dict(probe.document_analysis)

    final_pages = list(embedded_pages)
    page_routes = ["embedded_text" if index not in suspicious_pages else "unresolved" for index in range(page_count)]
    ocr_pages: list[int] = []
    unresolved_pages: list[int] = []
    ocr_unavailable = False
    orientation_retries: list[int] = []
    if suspicious_pages and ocr_mode != "off":
        for index in sorted(suspicious_pages):
            _raise_if_cancelled(cancelled)
            recovered = (
                _ocr_pdf_page(data, index, ocr_languages)
                if cancelled is None
                else _ocr_pdf_page(
                    data, index, ocr_languages, cancelled=cancelled
                )
            )
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
            elif not recovered.nonprose_or_blank or _pdf_text_has_damaged_numeral(
                embedded_pages[index]
            ):
                unresolved_pages.append(index + 1)
                page_routes[index] = "unresolved"
            else:
                # No lexical OCR output is consistent with a blank cover,
                # divider, or illustration page. Such pages do not make an
                # otherwise substantive document incomplete.
                page_routes[index] = "nonprose_or_blank"
    elif suspicious_pages:
        for index in sorted(suspicious_pages):
            if not _pdf_text_has_damaged_numeral(
                embedded_pages[index]
            ) and _pdf_page_is_nonprose(data, index):
                page_routes[index] = "nonprose_or_blank"
            else:
                page_routes[index] = "unresolved"
                unresolved_pages.append(index + 1)

    for page_number in unresolved_pages:
        if _pdf_text_has_damaged_numeral(embedded_pages[page_number - 1]):
            # Keep the page marker and coverage failure, but do not invite the
            # source reader to reconstruct a value from known damaged text.
            final_pages[page_number - 1] = ""
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


def _ocr_pdf_page(
    data: bytes,
    page_index: int,
    languages: tuple[str, ...],
    *,
    cancelled: Callable[[], bool] | None = None,
) -> _OCRPageResult:
    try:
        _raise_if_cancelled(cancelled)
        with tempfile.TemporaryDirectory(prefix="auto-zettelkasten-ocr-") as temporary:
            temporary_root = Path(temporary)
            rendered = (
                _render_pdf_page(data, page_index, temporary_root)
                if cancelled is None
                else _render_pdf_page(
                    data, page_index, temporary_root, cancelled=cancelled
                )
            )
            if rendered is None:
                return _OCRPageResult()
            image_path, renderer = rendered
            tesseract = shutil.which("tesseract")
            if not tesseract:
                return _OCRPageResult()
            language = "+".join(language.strip() for language in languages if language.strip()) or "eng"
            first = (
                _run_tesseract(
                    tesseract, image_path, language=language, psm=3
                )
                if cancelled is None
                else _run_tesseract(
                    tesseract,
                    image_path,
                    language=language,
                    psm=3,
                    cancelled=cancelled,
                )
            )
            orientation_retry = _rendered_page_is_landscape(image_path)
            if not _page_text_is_suspicious(first) and not orientation_retry:
                return _OCRPageResult(
                    text=first,
                    route=f"{renderer}_tesseract",
                    available=True,
                )
            # PSM 1 is the single orientation-aware retry for a failed or
            # implausible first pass.
            second = (
                _run_tesseract(
                    tesseract, image_path, language=language, psm=1
                )
                if cancelled is None
                else _run_tesseract(
                    tesseract,
                    image_path,
                    language=language,
                    psm=1,
                    cancelled=cancelled,
                )
            )
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
    except ExtractionCancelled:
        raise
    except (OSError, subprocess.SubprocessError, RuntimeError):
        return _OCRPageResult()


def _render_pdf_page(
    data: bytes,
    page_index: int,
    temporary_root: Path,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> tuple[Path, str] | None:
    _raise_if_cancelled(cancelled)
    try:
        import pypdfium2 as pdfium

        while True:
            _raise_if_cancelled(cancelled)
            if _PDFIUM_RENDER_LOCK.acquire(timeout=0.1):
                break
        try:
            _raise_if_cancelled(cancelled)
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
        finally:
            _PDFIUM_RENDER_LOCK.release()
        return image_path, "pdfium"
    except ExtractionCancelled:
        raise
    except (ImportError, OSError, RuntimeError, ValueError):
        pass

    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm:
        return None
    source_path = temporary_root / "source.pdf"
    source_path.write_bytes(data)
    output_root = temporary_root / f"page-{page_index + 1}"
    completed = _run_cancellable(
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
        text=False,
        timeout=120,
        cancelled=cancelled,
    )
    image_path = output_root.with_suffix(".png")
    return (image_path, "poppler") if completed.returncode == 0 and image_path.exists() else None


def _pdf_renderer_version(renderer: str) -> str:
    if renderer == "pdfium":
        try:
            return importlib.metadata.version("pypdfium2")
        except importlib.metadata.PackageNotFoundError:
            return "unknown"
    return "system" if renderer == "poppler" else "unknown"


def render_pdf_pages(
    data: bytes,
    page_numbers: Sequence[int],
    output_dir: Path,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> list[PDFPageImage]:
    """Render selected 1-based PDF pages as canonical, bounded PNG files."""

    requested = list(page_numbers)
    if len(requested) > PDF_PAGE_IMAGE_MAX_COUNT:
        raise ValueError(
            f"at most {PDF_PAGE_IMAGE_MAX_COUNT} PDF pages may be rendered"
        )
    if any(
        isinstance(page_number, bool)
        or not isinstance(page_number, int)
        or page_number <= 0
        for page_number in requested
    ):
        raise ValueError("PDF page numbers must be positive 1-based integers")
    if len(set(requested)) != len(requested):
        raise ValueError("PDF page numbers must be unique")
    ordered = sorted(requested)
    if not ordered:
        return []

    from PIL import Image

    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    rendered_pages: list[PDFPageImage] = []
    for page_number in ordered:
        _raise_if_cancelled(cancelled)
        with tempfile.TemporaryDirectory(
            prefix=".auto-zettelkasten-pdf-page-",
            dir=output_root,
        ) as temporary:
            temporary_root = Path(temporary)
            rendered = _render_pdf_page(
                data,
                page_number - 1,
                temporary_root,
                cancelled=cancelled,
            )
            if rendered is None:
                raise RuntimeError(f"pdf_page_render_failed:{page_number}")
            raw_path, renderer = rendered
            canonical_path = temporary_root / "canonical.png"
            _raise_if_cancelled(cancelled)
            with Image.open(raw_path) as source:
                image = source.convert("RGB")
            try:
                if max(image.size) > PDF_PAGE_IMAGE_MAX_SIDE:
                    image.thumbnail(
                        (PDF_PAGE_IMAGE_MAX_SIDE, PDF_PAGE_IMAGE_MAX_SIDE),
                        Image.Resampling.LANCZOS,
                    )
                width, height = image.size
                image.save(
                    canonical_path,
                    format="PNG",
                    optimize=False,
                    compress_level=9,
                )
            finally:
                image.close()
            _raise_if_cancelled(cancelled)
            final_path = output_root / f"page-{page_number:04d}.png"
            canonical_path.replace(final_path)

        rendered_pages.append(
            PDFPageImage(
                page_number=page_number,
                path=final_path,
                media_type="image/png",
                width=width,
                height=height,
                sha256=hashlib.sha256(final_path.read_bytes()).hexdigest(),
                byte_count=final_path.stat().st_size,
                renderer=renderer,
                renderer_version=_pdf_renderer_version(renderer),
                render_policy_version=PDF_PAGE_IMAGE_RENDER_POLICY_VERSION,
            )
        )
    return rendered_pages


def _run_tesseract(
    tesseract: str,
    image_path: Path,
    *,
    language: str,
    psm: int,
    cancelled: Callable[[], bool] | None = None,
) -> str:
    completed = _run_cancellable(
        [tesseract, str(image_path), "stdout", "-l", language, "--psm", str(psm)],
        text=True,
        timeout=120,
        cancelled=cancelled,
    )
    return _clean_text(completed.stdout) if completed.returncode == 0 else ""


def _raise_if_cancelled(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise ExtractionCancelled("extraction_cancelled")


def _run_cancellable(
    command: list[str],
    *,
    text: bool,
    timeout: float,
    cancelled: Callable[[], bool] | None,
) -> subprocess.CompletedProcess[Any]:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
    )
    started = time.monotonic()
    try:
        while True:
            _raise_if_cancelled(cancelled)
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                stdout, stderr = process.communicate(timeout=min(0.2, remaining))
                return subprocess.CompletedProcess(
                    command, process.returncode, stdout, stderr
                )
            except subprocess.TimeoutExpired:
                continue
    except BaseException:
        process.terminate()
        try:
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
        raise


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


def _pdf_text_has_damaged_numeral(text: str) -> bool:
    # ponytail: only unmistakable O/comma numeral damage; other glyph errors
    # need separate evidence before extending local OCR selection.
    return any(
        "O" in token
        and (token.startswith("~") or any(char.isdigit() for char in token))
        for token in re.findall(
            r"(?<![\w,])~?[0-9O]{1,3}(?:,[0-9O]{3})+(?!\w|,[0-9O])", text
        )
    )


def _page_text_is_suspicious(text: str, *, repeated_units: set[str] | None = None) -> bool:
    if _pdf_text_has_damaged_numeral(text):
        return True
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
    if not normalized or _pdf_text_has_damaged_numeral(normalized) or any(
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
        str(index): str(label)
        for index in range(1, page_count + 1)
        if (label := supplied.get(str(index)) or supplied.get(index))
    }


def is_ambiguous_pdf_heading(label: str) -> bool:
    # ponytail: initials can mimic nested Roman headings; omit ambiguous hints until layout disambiguates them.
    return bool(re.match(r"^[IVXLCDM]\.\s+[A-Z]\.\s", label))


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
                "printed_page": str(printed_page_map.get(str(page)) or ""),
            }
            object_match = object_heading.match(line)
            if object_match:
                target = (
                    tables
                    if object_match.group("kind").casefold() == "table"
                    else figures
                )
                target.append(span)
            elif (
                known_heading.match(line) or numbered_heading.match(line)
            ) and not is_ambiguous_pdf_heading(line):
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


def _parse_html(raw_html: str, *, preserve_tables: bool = False) -> _HTMLTextExtractor:
    parser = _HTMLTextExtractor(preserve_tables=preserve_tables)
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


def _pdf_embedded_text(page: Any) -> str:
    """Restore geometry-backed spaces without replacing plain text or its order."""
    plain = _clean_text(page.extract_text() or "")
    # ponytail: cap quadratic alignment at 20K characters per page; dense pages
    # retain plain extraction until a bounded alignment implementation is needed.
    if not plain or len(plain) > 20_000:
        return plain
    try:
        layout = _clean_text(page.extract_text(extraction_mode="layout") or "")
    except ExtractionCancelled:
        raise
    except Exception:
        return plain
    if not layout or len(layout) > 20_000 or layout == plain:
        return plain
    plain_offsets = [i for i, char in enumerate(plain) if not char.isspace()]
    layout_offsets = [i for i, char in enumerate(layout) if not char.isspace()]
    plain_chars = "".join(plain[i] for i in plain_offsets)
    layout_chars = "".join(layout[i] for i in layout_offsets)
    insertions: set[int] = set()
    blocks = (
        [(0, 0, len(plain_chars))]
        if plain_chars == layout_chars
        else SequenceMatcher(None, plain_chars, layout_chars, autojunk=False).get_matching_blocks()
    )
    for plain_start, layout_start, size in blocks:
        shared = plain_chars[plain_start:plain_start + size]
        if size < 32 or plain_chars.count(shared) != 1 or layout_chars.count(shared) != 1:
            continue
        for offset in range(1, size):
            left, right = plain_start + offset, layout_start + offset
            if (
                layout_offsets[right] > layout_offsets[right - 1] + 1
                and plain_offsets[left] == plain_offsets[left - 1] + 1
            ):
                insertions.add(plain_offsets[left])
    return "".join((" " if i in insertions else "") + char for i, char in enumerate(plain))


def _clean_text(value: str) -> str:
    value = value.replace("\x00", " ")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n\s*\n\s*\n+", "\n\n", value)
    return value.strip()
