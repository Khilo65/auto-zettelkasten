from io import BytesIO

import pytest
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, FloatObject, NameObject, RectangleObject

from auto_zettelkasten.annotation_previews import (
    AnnotationPreviews, MAX_EDGE, MAX_PIXELS, render_previews, verified_pdf_stream, visual_identity,
)
from auto_zettelkasten.documents import DocumentService
from auto_zettelkasten.zotero_workspace import VerifiedPDF


def pdf_bytes(*, rotation=0, crop=False, unit=1, pages=1, width=200, height=300):
    writer = PdfWriter()
    for _ in range(pages):
        page = writer.add_blank_page(width=width, height=height)
        content = DecodedStreamObject()
        content.set_data(b"0 0 0 rg 20 40 80 20 re f")
        page[NameObject("/Contents")] = writer._add_object(content)
        if crop:
            page[NameObject("/CropBox")] = RectangleObject([10, 20, 190, 280])
        if rotation:
            page.rotate(rotation)
        if unit != 1:
            page[NameObject("/UserUnit")] = FloatObject(unit)
    output = BytesIO(); writer.write(output)
    return output.getvalue()


def area(key="AREATEST", *, kind="image", color="#ffd400"):
    position = {"pageIndex": 0, "rects": [[20, 40, 100, 60]]}
    if kind == "ink":
        position = {"pageIndex": 0, "paths": [[30, 50, 90, 50]], "width": 2}
    return {"id": key, "key": key, "type": kind, "position": position, "color": color,
            "scope": "users/0", "attachment_key": "PDFTEST2", "parentItem": "PDFTEST2", "version": 2,
            "text": "Text quotation", "comment": "Personal interpretation", "pageLabel": "458", "sortIndex": "00000|000001|00100", "tags": []}


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("crop", [False, True])
def test_crop_geometry_rotation_and_nonzero_cropbox(rotation, crop):
    result = render_previews(BytesIO(pdf_bytes(rotation=rotation, crop=crop)), area())
    assert len(result) == 1 and result[0]["page_index"] == 0
    image = Image.open(BytesIO(result[0]["bytes"]))
    assert image.getpixel((image.width // 2, image.height // 2)) == (0, 0, 0)
    expected = (320, 80) if rotation in {0, 180} else (80, 320)
    assert all(abs(actual - target) <= 2 for actual, target in zip(image.size, expected))


def test_userunit_scales_output_and_extreme_regions_stay_bounded():
    basic = Image.open(BytesIO(render_previews(BytesIO(pdf_bytes()), area())[0]["bytes"]))
    unit = Image.open(BytesIO(render_previews(BytesIO(pdf_bytes(unit=3)), area())[0]["bytes"]))
    assert abs(unit.width - basic.width * 3) <= 4
    large = area(); large["position"]["rects"] = [[0, 0, 10000, 10000]]
    image = Image.open(BytesIO(render_previews(BytesIO(pdf_bytes(width=10000, height=10000)), large)[0]["bytes"]))
    assert max(image.size) <= MAX_EDGE and image.width * image.height <= MAX_PIXELS


@pytest.mark.parametrize("kind", ["ink", "highlight", "underline"])
def test_visual_overlay_and_multiline_highlight_preserve_underlying_page(kind):
    row = area(kind=kind, color="#ff0000")
    image = Image.open(BytesIO(render_previews(BytesIO(pdf_bytes()), row)[0]["bytes"]))
    pixels = zip(*[iter(image.tobytes())] * 3)
    assert any(r > g + 30 and r > b + 30 for r, g, b in pixels)
    assert image.width <= MAX_EDGE and image.height <= MAX_EDGE


def test_multi_page_highlight_is_two_separate_located_images():
    row = area(kind="highlight")
    row["position"]["nextPageRects"] = [[20, 40, 100, 60]]
    result = render_previews(BytesIO(pdf_bytes(pages=2)), row)
    assert [entry["page_index"] for entry in result] == [0, 1]
    assert render_previews(BytesIO(pdf_bytes(pages=2)), row, page_budget=1) == []
    with pytest.raises(ValueError, match="not present"):
        render_previews(BytesIO(pdf_bytes()), row)


def test_visual_identity_ignores_comments_versions_labels_and_area_color():
    first = area()
    assert visual_identity(first) == visual_identity({**first, "version": 999, "comment": "changed", "pageLabel": "ix", "color": "#ff0000"})
    ink = area(kind="ink")
    assert visual_identity(ink) != visual_identity({**ink, "color": "#ff0000"})
    with pytest.raises(ValueError):
        visual_identity({**ink, "color": "url(https://bad)"})


@pytest.fixture
def context(tmp_path):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    (workspace / "notes").mkdir(); (workspace / "notes" / "Note.md").write_text("Human prose.\n")
    documents = DocumentService(journal_root=tmp_path / "operations", override_root=tmp_path / "overrides")
    opened = documents.open(workspace)
    source = tmp_path / "source.pdf"; source.write_bytes(pdf_bytes())
    info = source.stat()
    pdf = VerifiedPDF(source, "PDFTEST2", "PARENT22", "users/0", "testserver22", 1, info.st_size, info.st_mtime_ns)
    previews = AnnotationPreviews(documents, tmp_path / "previews")
    yield workspace, documents, opened["id"], source, pdf, previews
    documents.close()


def test_cache_reuse_preferences_restart_missing_asset_and_comment_only_changes(context, monkeypatch):
    workspace, documents, ws, _, pdf, previews = context
    row = area()
    first = previews.prepare(ws, "notes/Note.md", pdf, [row])
    assert first["rendered"] == 1 and first["status"] == "current"
    path = workspace / first["items"][row["id"]][0]["path"]
    assert path.is_file()
    timestamp = path.stat().st_mtime_ns
    second = previews.prepare(ws, "notes/Note.md", pdf, [{**row, "comment": "Changed", "version": 3}])
    assert second["rendered"] == 0 and second["visual_hash"] == first["visual_hash"]
    assert path.stat().st_mtime_ns == timestamp
    path.unlink()
    assert previews.prepare(ws, "notes/Note.md", pdf, [row], render=False)["status"] == "pending"
    assert previews.prepare(ws, "notes/Note.md", pdf, [row])["rendered"] == 1
    highlight = area("HIGHLGHT", kind="highlight")
    assert not previews.prepare(ws, "notes/Note.md", pdf, [highlight])["items"]
    previews.preference(ws, pdf, highlight["id"], True)
    reopened = AnnotationPreviews(documents, previews.root)
    assert reopened.prepare(ws, "notes/Note.md", pdf, [highlight])["items"]
    reopened.preference(ws, pdf, highlight["id"], False)
    assert not reopened.prepare(ws, "notes/Note.md", pdf, [highlight])["items"]


def test_batch_limit_and_same_keys_in_different_libraries_do_not_collide(context):
    _, _, ws, _, pdf, previews = context
    rows = [area(f"AREA{i:04d}") for i in range(10)]
    first = previews.prepare(ws, "notes/Note.md", pdf, rows)
    assert first["rendered"] == 8 and first["pending_count"] == 2 and first["status"] == "pending"
    second = previews.prepare(ws, "notes/Note.md", pdf, rows)
    assert second["rendered"] == 2 and len(second["items"]) == 10
    other = VerifiedPDF(pdf.path, pdf.attachment_key, pdf.parent_item, "groups/42", pdf.server_id, pdf.version, pdf.size, pdf.mtime_ns)
    assert previews.prepare(ws, "notes/Note.md", other, [rows[0]])["items"] != {rows[0]["id"]: second["items"][rows[0]["id"]]}


def test_replaced_pdf_and_symlinks_never_supply_images(context):
    _, _, ws, source, pdf, previews = context
    source.write_bytes(b"%PDF changed")
    result = previews.prepare(ws, "notes/Note.md", pdf, [area()])
    assert result["status"] == "unavailable" and not result["items"]
    link = source.parent / "link.pdf"; link.symlink_to(source)
    info = source.stat()
    linked = VerifiedPDF(link, pdf.attachment_key, pdf.parent_item, pdf.scope, pdf.server_id, 1, info.st_size, info.st_mtime_ns)
    with pytest.raises(ValueError, match="direct verified"):
        with verified_pdf_stream(linked):
            pass


def test_one_pdf_handle_per_bounded_batch_and_restart_after_asset_write(context, monkeypatch):
    import auto_zettelkasten.annotation_previews as module
    from auto_zettelkasten.workspace_media import WorkspaceMedia
    workspace, documents, ws, _, pdf, previews = context
    opened = []
    enter = module._PreviewRenderer.__enter__
    def observed(renderer):
        opened.append(True)
        return enter(renderer)
    monkeypatch.setattr(module._PreviewRenderer, "__enter__", observed)
    rows = [area(f"AREA{i:04d}") for i in range(3)]
    assert previews.prepare(ws, "notes/Note.md", pdf, rows)["rendered"] == 3
    assert len(opened) == 1
    old = WorkspaceMedia.write_generated
    def interrupted(media, *args, **kwargs):
        old(media, *args, **kwargs)
        raise OSError("interrupted after PNG write")
    monkeypatch.setattr(WorkspaceMedia, "write_generated", interrupted)
    result = previews.prepare(ws, "notes/Note.md", pdf, [area("NEXTAREA")])
    assert result["status"] == "unavailable" and result["items"] == {}
    assets = set(workspace.rglob("*.png"))
    monkeypatch.setattr(WorkspaceMedia, "write_generated", old)
    restarted = AnnotationPreviews(documents, previews.root)
    recovered = restarted.prepare(ws, "notes/Note.md", pdf, [area("NEXTAREA")])
    assert recovered["status"] == "current" and len(recovered["items"]) == 1
    assert set(workspace.rglob("*.png")) == assets


def test_inflight_pdf_change_discards_preview_result_without_deleting_recovery_asset(context, monkeypatch):
    import auto_zettelkasten.annotation_previews as module
    workspace, _, ws, source, pdf, previews = context
    original = module._PreviewRenderer.render
    def changed(renderer, *args, **kwargs):
        result = original(renderer, *args, **kwargs)
        source.write_bytes(source.read_bytes() + b"\n% changed\n")
        return result
    monkeypatch.setattr(module._PreviewRenderer, "render", changed)
    result = previews.prepare(ws, "notes/Note.md", pdf, [area()])
    assert result["status"] == "unavailable" and result["items"] == {}
    assert list(workspace.rglob("*.png"))
