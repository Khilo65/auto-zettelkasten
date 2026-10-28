"""Local, bounded images derived from verified Zotero PDF annotations.

Preview requests are not annotation mutations. PNGs are ordinary workspace
assets; private records retain preferences and freshness, never PDF copies.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
import hashlib
import io
from importlib.metadata import version as package_version
import json
import math
import os
from pathlib import Path
import re
import stat
import threading

from pypdf.errors import PdfReadError

from .documents import DocumentError
from .zotero_annotations import AnnotationError, validate_position

POLICY = "annotation-preview-v1"
MAX_EDGE = 4096
MAX_PIXELS = 8_000_000
BATCH_SIZE = 8
_MAX_RECORDS = 20000


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def visual_identity(annotation):
    """Comments, transport versions and page labels do not change image bytes."""
    kind = annotation.get("type")
    position = validate_position(annotation.get("position"), kind)
    color = annotation.get("color", "#ffd400")
    if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise AnnotationError("Invalid annotation preview color.")
    return {"type": kind, "position": position, "color": color.lower() if kind in {"ink", "highlight", "underline"} else None}


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@contextmanager
def verified_pdf_stream(pdf):
    """Read the already verified attachment through one non-following handle."""
    path = Path(pdf.path)
    if not path.is_absolute() or any(p.is_symlink() for p in [path, *path.parents]):
        raise ValueError("Annotation previews require a direct verified PDF file.")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or (before.st_size, before.st_mtime_ns) != (pdf.size, pdf.mtime_ns):
            raise ValueError("The PDF changed before generating its annotation image.")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            yield stream, _stamp(before)
        if _stamp(os.fstat(fd)) != _stamp(before) or _stamp(path.lstat()) != _stamp(before):
            raise ValueError("The PDF changed while generating its annotation image.")
    finally:
        os.close(fd)


def _rect_bounds(rects):
    return min(r[0] for r in rects), min(r[1] for r in rects), max(r[2] for r in rects), max(r[3] for r in rects)


def _page_preview(document, page_index, rects, annotation, user_unit):
    """Render only the bounded region, not a whole high-resolution book page."""
    import pypdfium2 as pdfium
    from PIL import Image, ImageColor, ImageDraw

    if page_index >= len(document):
        raise ValueError("The annotation page is not present in this PDF.")
    page = document[page_index]
    bitmap = None
    try:
        width, height = page.get_size()
        if not all(math.isfinite(v) and 0 < v <= 1_000_000 for v in (width, height)):
            raise ValueError("Invalid PDF page dimensions.")
        kind, position = annotation["type"], annotation["position"]
        if kind == "ink":
            points = [p for path in position["paths"] for p in zip(path[::2], path[1::2])]
            left, right = min(p[0] for p in points), max(p[0] for p in points)
            bottom, top = min(p[1] for p in points), max(p[1] for p in points)
            padding = max(10, position["width"] / 2)
            left, right = left - padding, right + padding
            bottom, top = bottom - padding, top + padding
            if right - left < 30:
                mid = (left + right) / 2; left, right = mid - 15, mid + 15
            if top - bottom < 30:
                mid = (top + bottom) / 2; bottom, top = mid - 15, mid + 15
            rects = [[left, bottom, right, top]]
        else:
            left, bottom, right, top = _rect_bounds(rects)
            if kind in {"highlight", "underline"}:
                rects = list(rects)
                left, bottom, right, top = left - 4, bottom - 4, right + 4, top + 4
        # PDFium's conversion accounts for the actual page rotation and CropBox.
        # Calculate bounds at scale one first; rendering crop is bitmap-space.
        conv_one = pdfium.PdfPosConv(page, (0, 0, math.ceil(width), math.ceil(height), 0))
        corners = [conv_one.to_bitmap(x, y) for x in (left, right) for y in (bottom, top)]
        x0, y0 = max(0, min(p[0] for p in corners)), max(0, min(p[1] for p in corners))
        x1, y1 = min(math.ceil(width), max(p[0] for p in corners)), min(math.ceil(height), max(p[1] for p in corners))
        if x1 <= x0 or y1 <= y0:
            raise ValueError("The annotation region is outside the PDF page.")
        region_width, region_height = x1 - x0, y1 - y0
        scale = min(4 * user_unit, MAX_EDGE / max(region_width, region_height),
                    math.sqrt(MAX_PIXELS / (region_width * region_height)), (2 ** 30) / max(width, height))
        # Leave one pixel for ceil rounding at each cropped edge.
        scale = max(0.00001, scale * 0.999)
        canvas_width, canvas_height = math.ceil(width * scale), math.ceil(height * scale)
        conv = pdfium.PdfPosConv(page, (0, 0, canvas_width, canvas_height, 0))
        corners = [conv.to_bitmap(x, y) for x in (left, right) for y in (bottom, top)]
        px0, py0 = max(0, min(p[0] for p in corners)), max(0, min(p[1] for p in corners))
        px1, py1 = min(canvas_width, max(p[0] for p in corners)), min(canvas_height, max(p[1] for p in corners))
        if px1 <= px0 or py1 <= py0:
            raise ValueError("The selected region is too small to render.")
        crop = (px0 / scale, (canvas_height - py1) / scale, (canvas_width - px1) / scale, py0 / scale)
        bitmap = page.render(scale=scale, crop=crop, draw_annots=False)
        image = bitmap.to_pil().convert("RGB")
        color = ImageColor.getrgb(annotation.get("color") or "#ffd400")
        # Conversion is expressed relative to the cropped output's top-left.
        def point(x, y):
            px, py = conv.to_bitmap(x, y)
            return px - px0, py - py0
        if kind == "ink":
            draw = ImageDraw.Draw(image)
            stroke = max(1, round(position["width"] * scale))
            radius = stroke / 2
            for path in position["paths"]:
                points = [point(x, y) for x, y in zip(path[::2], path[1::2])]
                if len(points) > 1:
                    draw.line(points, fill=color, width=stroke, joint="curve")
                for x, y in points:
                    draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color)
        elif kind in {"highlight", "underline"}:
            overlay = Image.new("RGBA", image.size)
            draw = ImageDraw.Draw(overlay)
            for r in rects:
                if kind == "highlight":
                    points = [point(r[0], r[1]), point(r[2], r[1]), point(r[2], r[3]), point(r[0], r[3])]
                    draw.polygon(points, fill=(*color, 70))
                else:
                    draw.line([point(r[0], r[1]), point(r[2], r[1])], fill=(*color, 255), width=max(1, round(scale)))
            image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
        if image.width > MAX_EDGE or image.height > MAX_EDGE or image.width * image.height > MAX_PIXELS:
            image.thumbnail((MAX_EDGE, MAX_EDGE))
            if image.width * image.height > MAX_PIXELS:
                factor = math.sqrt(MAX_PIXELS / (image.width * image.height))
                image = image.resize((max(1, int(image.width * factor)), max(1, int(image.height * factor))))
        output = io.BytesIO()
        image.save(output, "PNG")
        return output.getvalue()
    finally:
        if bitmap is not None:
            bitmap.close()
        page.close()


class _PreviewRenderer:
    """One PDF handle and one PDFium lock for a bounded region batch."""
    def __init__(self, stream):
        self.stream = stream
        self.document = None

    def __enter__(self):
        import pypdfium2 as pdfium
        from pypdf import PdfReader
        from .extraction import _PDFIUM_RENDER_LOCK
        self.lock = _PDFIUM_RENDER_LOCK
        self.lock.acquire()
        try:
            self.stream.seek(0)
            self.reader = PdfReader(self.stream, strict=False)
            self.stream.seek(0)
            self.document = pdfium.PdfDocument(self.stream)
            return self
        except BaseException:
            self.lock.release()
            raise

    def __exit__(self, *args):
        try:
            if self.document is not None:
                self.document.close()
        finally:
            self.lock.release()

    def render(self, annotation, *, page_budget=2):
        annotation = {**annotation, **visual_identity(annotation)}
        if annotation["type"] not in {"image", "ink", "highlight", "underline"}:
            raise ValueError("This annotation has no image preview.")
        position = annotation["position"]
        pages = [(position["pageIndex"], position.get("rects"))]
        if position.get("nextPageRects"):
            pages.append((position["pageIndex"] + 1, position["nextPageRects"]))
        if len(pages) > page_budget:
            return []
        result = []
        for index, rects in pages:
            if index >= len(self.reader.pages):
                raise ValueError("The annotation page is not present in this PDF.")
            unit = float(self.reader.pages[index].get("/UserUnit", 1))
            if not math.isfinite(unit) or not 0 < unit <= 75000:
                raise ValueError("Invalid PDF user unit.")
            result.append({"page_index": index, "bytes": _page_preview(self.document, index, rects, annotation, unit)})
        return result


def render_previews(stream, annotation, *, page_budget=2):
    """Return PNG/page pairs. No files, Zotero records or model calls are made."""
    with _PreviewRenderer(stream) as renderer:
        return renderer.render(annotation, page_budget=page_budget)


class AnnotationPreviews:
    def __init__(self, documents, private_root):
        self.documents = documents
        from .zotero_workspace_service import _private_directory
        self.root = _private_directory(Path(private_root))
        self._lock = threading.RLock()
        self._pdf_hashes = {}
        self.runtime = [package_version(name) for name in ("pypdfium2", "pypdf", "Pillow")]

    def _context(self, workspace_id):
        from .documents import _identity
        from .zotero_workspace_service import _private_directory
        root = Path(self.documents.workspace_path(workspace_id))
        identity = list(_identity(root.lstat()))
        key = _digest([str(root), identity])
        return _private_directory(self.root / key) / "previews.json", key

    def _load(self, path, workspace_key):
        from .zotero_workspace_service import _read_json
        value = _read_json(path) or {"schema_version": 1, "workspace": workspace_key, "preferences": {}, "records": {}}
        if (value.get("schema_version") != 1 or value.get("workspace") != workspace_key
                or not isinstance(value.get("preferences"), dict) or not isinstance(value.get("records"), dict)
                or len(value["preferences"]) > _MAX_RECORDS or len(value["records"]) > _MAX_RECORDS):
            raise ValueError("Invalid private annotation-preview inventory.")
        return value

    @staticmethod
    def _annotation_id(pdf, annotation_key):
        return _digest([pdf.server_id, pdf.scope, pdf.attachment_key, annotation_key])

    def preferences(self, workspace_id, pdf, annotations):
        from .zotero_workspace_service import _receipt_guard
        path, workspace_key = self._context(workspace_id)
        with self._lock, _receipt_guard(path):
            state = self._load(path, workspace_key)
            return {row["id"]: state["preferences"].get(self._annotation_id(pdf, row["id"])) is True
                    for row in annotations if row.get("type") in {"highlight", "underline"}}

    def preference(self, workspace_id, pdf, annotation_key, enabled):
        from .zotero_workspace_service import _receipt_guard, _write_json
        if type(enabled) is not bool:
            raise ValueError("The image-preview preference requires a boolean.")
        path, workspace_key = self._context(workspace_id)
        with self._lock, _receipt_guard(path):
            state = self._load(path, workspace_key)
            state["preferences"][self._annotation_id(pdf, annotation_key)] = enabled
            if len(state["preferences"]) > _MAX_RECORDS:
                raise ValueError("The annotation-preview preference inventory is full.")
            _write_json(path, state)

    def prepare(self, workspace_id, note_path, pdf, annotations, *, render=True):
        """Return only fresh assets; unavailable derivatives never fail a save."""
        from .workspace_media import WorkspaceMedia
        from .zotero_workspace_service import _receipt_guard, _write_json
        path, workspace_key = self._context(workspace_id)
        media = WorkspaceMedia(self.documents)
        with self._lock, _receipt_guard(path):
            state = self._load(path, workspace_key)
            selected = [row for row in annotations if row["type"] in {"image", "ink"}
                        or row["type"] in {"highlight", "underline"} and state["preferences"].get(self._annotation_id(pdf, row["id"])) is True]
            if not selected:
                return {"items": {}, "status": "current", "reason": "", "visual_hash": _digest([POLICY, []]), "pending_count": 0, "rendered": 0}
            result = {"items": {}, "status": "current", "reason": "", "pending_count": 0, "rendered": 0}
            visual_keys = []
            try:
                with verified_pdf_stream(pdf) as (stream, stamp), ExitStack() as stack:
                    renderer = None
                    hash_key = (str(pdf.path), stamp)
                    content_hash = self._pdf_hashes.get(hash_key)
                    if content_hash is None:
                        digest = hashlib.sha256()
                        while chunk := stream.read(1024 * 1024):
                            digest.update(chunk)
                        content_hash = digest.hexdigest()
                        if len(self._pdf_hashes) >= 64:
                            self._pdf_hashes.pop(next(iter(self._pdf_hashes)))
                        self._pdf_hashes[hash_key] = content_hash
                    for row in selected:
                        base = self._annotation_id(pdf, row["id"])
                        key = _digest([POLICY, self.runtime, base, content_hash, visual_identity(row)])
                        visual_keys.append(key)
                        cached = state["records"].get(key)
                        assets = cached.get("assets", []) if isinstance(cached, dict) and cached.get("identity") == base else []
                        if assets:
                            try:
                                assets = [{**asset, **media.read(workspace_id, asset["path"], revision=asset["revision"])[1]} for asset in assets]
                            except (DocumentError, ValueError, OSError, KeyError):
                                assets = []
                        if not assets and render:
                            needed = 2 if row["position"].get("nextPageRects") else 1
                            if result["rendered"] + needed <= BATCH_SIZE:
                                if renderer is None:
                                    renderer = stack.enter_context(_PreviewRenderer(stream))
                                rendered = renderer.render(row, page_budget=needed)
                                assets = []
                                for item in rendered:
                                    metadata = media.write_generated(workspace_id, note_path, item["bytes"], identity=_digest([key, item["page_index"]]))
                                    assets.append({"path": metadata["path"], "revision": metadata["revision"], "page_index": item["page_index"]})
                                result["rendered"] += len(rendered)
                                state["records"][key] = {"identity": base, "assets": assets}
                        if assets:
                            result["items"][row["id"]] = [{"path": asset["path"], "page_index": asset["page_index"]} for asset in assets]
                        else:
                            result["pending_count"] += 1
            except (ValueError, OSError, RuntimeError, ImportError, PdfReadError) as exc:
                result["items"] = {}
                result.update(status="unavailable", reason="Image preview pending: " + str(exc), pending_count=max(1, len(selected) - len(result["items"])))
            if len(state["records"]) > _MAX_RECORDS:
                # Assets remain untouched. Old cache bookkeeping may be rebuilt.
                for key in list(state["records"])[:len(state["records"]) - _MAX_RECORDS]:
                    del state["records"][key]
            _write_json(path, state)
            if result["status"] == "current" and result["pending_count"]:
                result.update(status="pending", reason="Image previews are waiting for the next bounded rendering batch.")
            result["visual_hash"] = _digest([POLICY, sorted(visual_keys), result["items"], result["status"]])
            return result
