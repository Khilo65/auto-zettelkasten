from io import BytesIO
import os

from PIL import Image
import pytest

from auto_zettelkasten.documents import DocumentError, DocumentService
from auto_zettelkasten.workspace_media import WorkspaceMedia, image_metadata, image_references


def raster(color="purple", format="PNG", size=(20, 10)):
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format=format)
    return output.getvalue()


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "notes"
    root.mkdir()
    (root / "Nested").mkdir()
    (root / "Nested/Note.md").write_text("# Note\n")
    service = DocumentService(journal_root=tmp_path / "recovery")
    identity = service.open(root)["id"]
    try:
        yield root, service, identity, WorkspaceMedia(service)
    finally:
        service.close()


def test_import_generated_dedup_and_workspace_revision_binding(workspace, tmp_path):
    root, service, identity, media = workspace
    data = raster()
    row = media.import_image(identity, "Nested/Note.md", data, filename="日本 語.png")
    assert row["path"].startswith("attachments/images/")
    assert row["relative_path"].startswith("../attachments/images/")
    assert row["mime"] == "image/png" and (row["width"], row["height"]) == (20, 10)
    assert media.read(identity, row["path"], revision=row["revision"])[0] == data
    assert media.import_image(identity, "Nested/Note.md", data, filename="other.png")["reused"]
    assert media.resolve(identity, "Nested/Note.md", row["relative_path"])["revision"] == row["revision"]
    generated = media.write_generated(identity, "Nested/Note.md", data, identity="server/library/pdf/annotation/version")
    assert generated["path"].startswith("attachments/zotero/")
    assert len(media.inventory(identity)["assets"]) == 2
    with pytest.raises(DocumentError, match="changed"):
        media.read(identity, row["path"], revision="wrong")
    other = tmp_path / "other"
    other.mkdir()
    service.open(other)
    with pytest.raises(DocumentError):
        media.read(identity, row["path"])
    assert (root / row["path"]).read_bytes() == data


def test_resolution_order_unicode_ambiguity_and_escape(workspace, tmp_path):
    root, _, identity, media = workspace
    for directory in ("Nested", "Other", "Third"):
        (root / directory).mkdir(exist_ok=True)
        (root / directory / "figure 日本.png").write_bytes(raster())
    local = media.resolve(identity, "Nested/Note.md", "figure%20%E6%97%A5%E6%9C%AC.png", wiki=True)
    assert local["path"] == "Nested/figure 日本.png"
    with pytest.raises(DocumentError, match="Several"):
        media.resolve(identity, "Top.md", "figure 日本.png", wiki=True)
    (root / "figure 日本.png").write_bytes(raster("red"))
    assert media.resolve(identity, "Top.md", "figure 日本.png", wiki=True)["path"] == "figure 日本.png"
    outside = tmp_path / "outside.png"
    outside.write_bytes(raster())
    for target in ("../../../outside.png", "file:///outside.png", "https://example.test/a.png", "/outside.png", "..\\outside.png", "%2Foutside.png"):
        with pytest.raises(DocumentError):
            media.resolve(identity, "Nested/Note.md", target, wiki=True)


def test_unavailable_duplicate_does_not_create_a_false_unique_filename(workspace):
    root, _, identity, media = workspace
    (root / "Other").mkdir()
    (root / "Nested/figure.png").write_bytes(raster())
    (root / "Other/figure.png").write_bytes(b"damaged")
    with pytest.raises(DocumentError, match="Several"):
        media.resolve(identity, "Top.md", "FIGURE.PNG", wiki=True)


@pytest.mark.skipif(os.name != "posix", reason="Descriptor-relative path protections")
def test_symlink_and_hardlink_assets_are_not_served(workspace, tmp_path):
    root, _, identity, media = workspace
    outside = tmp_path / "outside.png"
    outside.write_bytes(raster())
    (root / "Linked.png").symlink_to(outside)
    os.link(outside, root / "Hard.png")
    (root / "External").symlink_to(tmp_path, target_is_directory=True)
    for target in ("Linked.png", "Hard.png", "External/outside.png"):
        with pytest.raises(DocumentError):
            media.read(identity, target)


def test_image_validation_rejects_mismatches_bombs_and_animation(monkeypatch):
    assert image_metadata(raster(format="JPEG"), filename="picture.jpeg")["mime"] == "image/jpeg"
    assert image_metadata(raster(format="WEBP"), filename="picture.webp")["mime"] == "image/webp"
    for data, filename in ((b"<svg></svg>", "picture.svg"), (raster(), "picture.jpg"), (b"broken", "broken.png")):
        with pytest.raises(DocumentError):
            image_metadata(data, filename=filename)
    monkeypatch.setattr("auto_zettelkasten.workspace_media.MAX_IMAGE_PIXELS", 100)
    with pytest.raises(DocumentError, match="40 megapixels"):
        image_metadata(raster())
    monkeypatch.setattr("auto_zettelkasten.workspace_media.MAX_IMAGE_PIXELS", 40_000_000)
    animated = BytesIO()
    Image.new("RGB", (4, 4), "red").save(animated, format="WEBP", save_all=True,
                                          append_images=[Image.new("RGB", (4, 4), "blue")], duration=50, loop=0)
    with pytest.raises(DocumentError, match="non-animated"):
        image_metadata(animated.getvalue())


def test_reference_spans_preserve_dimensions_and_ignore_examples():
    text = """![diagram](<../assets/日本 figure.png>)
![[assets/crop.png|400x200]]
[open](assets/crop.png)
`![ignore](missing.png)`
```
![[ignored.png]]
```
![remote](https://example.test/pixel.png)
"""
    references = image_references(text)
    assert [row["target"] for row in references] == ["../assets/日本 figure.png", "assets/crop.png", "assets/crop.png"]
    assert all(text[row["from"]:row["to"]] == row["target"] for row in references)


def test_reference_style_images_reuse_one_definition_destination():
    text = "![diagram][figure]\n![figure][]\n![figure]\n[figure]: <assets/figure 日本.png> \"caption\"\n[unused]: missing.png\n"
    references = image_references(text)
    assert len(references) == 1
    assert references[0]["target"] == "assets/figure 日本.png"
    assert text[references[0]["from"]:references[0]["to"]] == "assets/figure 日本.png"


def test_non_rendered_metadata_comments_quoted_code_and_escapes_are_not_assets():
    text = "---\nexample: '![[metadata.png]]'\n---\n<!-- ![[comment.png]] -->\n> ```\n> ![[code.png]]\n> ```\n\\![[escaped.png]]\n![valid](valid.png)"
    assert [row["target"] for row in image_references(text)] == ["valid.png"]


def test_reviewed_folder_move_preserves_images_and_repairs_relative_references(workspace):
    root, service, identity, media = workspace
    (root / "Nested/figure.png").write_bytes(raster())
    (root / "Outside.md").write_text("Emoji 📝 ![figure](Nested/figure.png)\n")
    (root / "Nested/Note.md").write_text("# Note\n![local](figure.png)\n![other](../Root.png)\n")
    (root / "Root.png").write_bytes(raster("blue"))
    (root / "Destination").mkdir()
    preview = service.prepare_relocation(identity, "Nested", "Destination/Renamed")
    assert len(preview["image_assets"]) == 2
    assert {row["path"] for row in preview["image_repairs"]} == {"Outside.md", "Nested/Note.md"}
    service.apply_relocation(identity, preview["token"], True, [])
    assert (root / "Destination/Renamed/figure.png").read_bytes() == raster()
    assert "Destination/Renamed/figure.png" in (root / "Outside.md").read_text()
    assert "../../Root.png" in (root / "Destination/Renamed/Note.md").read_text()
    assert media.resolve(identity, "Destination/Renamed/Note.md", "figure.png")["path"] == "Destination/Renamed/figure.png"


def test_generated_asset_folder_and_changed_reviewed_images_are_protected(workspace):
    root, service, identity, media = workspace
    media.write_generated(identity, "Nested/Note.md", raster(), identity="annotation")
    with pytest.raises(DocumentError, match="Generated annotation"):
        service.prepare_relocation(identity, "attachments/zotero", "Moved")
    (root / "Nested/figure.png").write_bytes(raster())
    preview = service.prepare_relocation(identity, "Nested", "Moved")
    (root / "Nested/figure.png").write_bytes(raster("blue"))
    with pytest.raises(DocumentError, match="changed after"):
        service.apply_relocation(identity, preview["token"], True, [])
    assert (root / "Nested/Note.md").exists()


def test_interrupted_folder_move_restores_image_and_note_paths(workspace, monkeypatch):
    root, service, identity, _ = workspace
    image = raster()
    (root / "Nested/figure.png").write_bytes(image)
    original = "![image](Nested/figure.png)\n"
    (root / "Outside.md").write_text(original)
    preview = service.prepare_relocation(identity, "Nested", "Moved")
    relocator = service._relocations()
    install = relocator._install
    injected = False

    def fail_after_write(*args, **kwargs):
        nonlocal injected
        result = install(*args, **kwargs)
        if not kwargs.get("rollback") and not injected:
            injected = True
            raise DocumentError("Injected interrupted publication")
        return result

    monkeypatch.setattr(relocator, "_install", fail_after_write)
    with pytest.raises(DocumentError, match="restored"):
        service.apply_relocation(identity, preview["token"], True, [])
    assert (root / "Nested/figure.png").read_bytes() == image
    assert (root / "Nested/Note.md").exists() and not (root / "Moved").exists()
    assert (root / "Outside.md").read_text() == original
    assert not relocator.blocked
