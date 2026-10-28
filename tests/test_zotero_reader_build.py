"""Synthetic checks for source identity and reader distribution metadata."""
import importlib.util
import json
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location(
    "build_zotero_reader", Path(__file__).parents[1] / "tools/build_zotero_reader.py"
)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def source_fixture(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "pdfjs").mkdir()
    (source / "pdfjs/pdf.js").mkdir()
    (source / ".zotero-locale-commit").write_text("locale-pin\n")
    pin = {"revision": "reader-pin", "submodules": {"pdfjs/pdf.js": "pdf-pin"},
           "locale_revision": "locale-pin"}
    states = {source: "reader-pin", source / "pdfjs/pdf.js": "pdf-pin"}
    monkeypatch.setattr(builder, "run", lambda args, cwd, env=None:
                        states[cwd] if "rev-parse" in args else "")
    return source, pin, states


def test_refuses_changed_submodule(tmp_path, monkeypatch):
    source, pin, states = source_fixture(tmp_path, monkeypatch)
    builder.verify_source(source, pin)
    states[source / "pdfjs/pdf.js"] = "different-revision"
    with pytest.raises(RuntimeError, match="submodule differs"):
        builder.verify_source(source, pin)


def test_refuses_modified_reader_and_locale(tmp_path, monkeypatch):
    source, pin, _ = source_fixture(tmp_path, monkeypatch)
    (source / ".zotero-locale-commit").write_text("other-locale")
    with pytest.raises(RuntimeError, match="Locale revision"):
        builder.verify_source(source, pin)
    (source / ".zotero-locale-commit").write_text("locale-pin")
    original = builder.run
    monkeypatch.setattr(builder, "run", lambda args, cwd, env=None:
                        " M source.js" if "status" in args and cwd == source else original(args, cwd))
    with pytest.raises(RuntimeError, match="modified source"):
        builder.verify_source(source, pin)


def test_incomplete_build_does_not_replace_assets(tmp_path, monkeypatch):
    source, pin, _ = source_fixture(tmp_path, monkeypatch)
    output = tmp_path / "assets"
    output.mkdir()
    previous = output / "reader.js"
    previous.write_text("previous verified build")
    with pytest.raises(RuntimeError, match="Incomplete reader"):
        builder.package_build(source, pin, output)
    assert previous.read_text() == "previous verified build"


def test_licenses_preserve_text_and_distinct_dependency_versions(tmp_path):
    root = tmp_path / "source"
    for relative, version in (("node_modules/library", "1.0"),
                              ("node_modules/parent/node_modules/library", "2.0")):
        package = root / relative
        package.mkdir(parents=True)
        (package / "package.json").write_text(json.dumps({"name": "library", "version": version, "license": "MIT"}))
        (package / "LICENSE").write_text(f"Copyright synthetic version {version}\nPermission granted.\n")
    inventory, notices = builder.license_inventory(root)
    assert [item["version"] for item in inventory] == ["1.0", "2.0"]
    assert "Copyright synthetic version 1.0" in notices
    assert "Copyright synthetic version 2.0" in notices
