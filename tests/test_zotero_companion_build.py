import json
from pathlib import Path
import runpy
import zipfile
from urllib.parse import urlsplit


def test_companion_xpi_is_reproducible_and_contains_only_original_runtime_sources(tmp_path):
    root = Path(__file__).resolve().parents[1]
    build = runpy.run_path(str(root / "tools/build_zotero_companion.py"))["build"]
    first, second = tmp_path / "first.xpi", tmp_path / "second.xpi"
    build(first)
    build(second)
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        assert archive.namelist() == ["bootstrap.js", "manifest.json"]
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["applications"]["zotero"]["strict_max_version"] == "10.0.*"
        settings = manifest["applications"]["zotero"]
        update_url = urlsplit(settings["update_url"])
        assert update_url.scheme == "https"
        assert update_url.hostname.endswith(".invalid")
        assert b"supportedMethods = [\"GET\"]" in archive.read("bootstrap.js")
