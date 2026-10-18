from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from auto_zettelkasten import pipeline
from auto_zettelkasten.files import read_yaml, write_yaml


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_state(root: Path) -> dict[str, tuple[bytes, str, int]]:
    result = {}
    for path in root.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            result[path.relative_to(root).as_posix()] = (
                data,
                _digest(data),
                path.stat().st_mtime_ns,
            )
    return result


def _checkpoint(tmp_path: Path, text: str) -> tuple[Path, dict]:
    source = tmp_path / "custody" / "synthetic-source.bin"
    source.parent.mkdir()
    source.write_bytes(b"Synthetic original source; separate from extracted text.\r\n")
    content = {
        "text": text,
        "content_hash": _digest(source.read_bytes()),
        "source_file": str(source),
        "content_route": "indexed_text",
        "media_type": "text/plain",
        "source_scope": "full_document",
        "attachment_key": "SYNTH001",
        "document_route": {"route_version": "1", "route": "synthetic_text"},
    }
    checkpoint = tmp_path / "checkpoint"
    pipeline._write_frozen_content(checkpoint, content)
    assert (checkpoint / "source.txt").read_bytes() == text.encode("utf-8")
    return checkpoint, content


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("First line.\nSecond line.\n", id="lf"),
        pytest.param("First line.\r\nSecond line.\r\n", id="crlf"),
        pytest.param("First line.\rSecond line.\r", id="bare-cr"),
        pytest.param("First.\r\nSecond.\rThird.\nFourth.", id="mixed"),
        pytest.param("Résumé — café, 東京, and 🧪.\n", id="utf8"),
        pytest.param("\ufeffRésumé 🧪.\r\nSecond line.\r", id="utf8-bom-mixed"),
    ],
)
def test_frozen_content_preserves_exact_text_hash_and_all_files(tmp_path: Path, text: str):
    checkpoint, original = _checkpoint(tmp_path, text)
    expected_hash = _digest(text.encode("utf-8"))
    assert read_yaml(checkpoint / "frozen_content.yml")["text_hash"] == expected_hash
    before = _file_state(tmp_path)

    try:
        loaded = pipeline._load_frozen_content(checkpoint)
    finally:
        assert _file_state(tmp_path) == before

    assert loaded == original
    assert loaded["text"] == text
    assert loaded["text"].encode("utf-8") == (checkpoint / "source.txt").read_bytes()
    assert _digest(loaded["text"].encode("utf-8")) == expected_hash
    assert loaded["content_hash"] == _digest(Path(loaded["source_file"]).read_bytes())


@pytest.mark.parametrize("original", ["First.\r\nSecond.\r\n", "First.\rSecond.\r"])
def test_frozen_content_rewrite_updates_exact_bytes_and_hash(tmp_path: Path, original: str):
    checkpoint, content = _checkpoint(tmp_path, original)
    replacement = "First.\nSecond.\n"
    pipeline._write_frozen_content(checkpoint, {**content, "text": replacement})

    assert (checkpoint / "source.txt").read_bytes() == replacement.encode("utf-8")
    assert pipeline._load_frozen_content(checkpoint)["text"] == replacement
    assert Path(content["source_file"]).read_bytes() == b"Synthetic original source; separate from extracted text.\r\n"


@pytest.mark.parametrize(
    "replacement",
    [
        pytest.param(b"First line.\nChanged line.\n", id="content-changed"),
        pytest.param(b"First line.\r\nSecond line.\r\n", id="lf-changed-to-crlf"),
        pytest.param(b"First line.\rSecond line.\r", id="lf-changed-to-bare-cr"),
    ],
)
def test_frozen_content_rejects_tampering_without_rewriting_files(tmp_path: Path, replacement: bytes):
    checkpoint, _ = _checkpoint(tmp_path, "First line.\nSecond line.\n")
    (checkpoint / "source.txt").write_bytes(replacement)
    before = _file_state(tmp_path)

    try:
        with pytest.raises(ValueError, match="hash mismatch"):
            pipeline._load_frozen_content(checkpoint)
    finally:
        assert _file_state(tmp_path) == before


def test_frozen_content_missing_source_is_rejected_without_recreating_it(tmp_path: Path):
    checkpoint, _ = _checkpoint(tmp_path, "Synthetic checkpoint.\n")
    (checkpoint / "source.txt").unlink()
    before = _file_state(tmp_path)

    try:
        with pytest.raises(ValueError, match="source text is missing"):
            pipeline._load_frozen_content(checkpoint)
    finally:
        assert _file_state(tmp_path) == before


@pytest.mark.parametrize("version", [None, "", "0", "2", "unknown"])
def test_frozen_content_rejects_bad_manifest_versions_without_changes(tmp_path: Path, version):
    checkpoint, _ = _checkpoint(tmp_path, "Synthetic checkpoint.\n")
    manifest = checkpoint / "frozen_content.yml"
    payload = read_yaml(manifest)
    payload["checkpoint_version"] = version
    write_yaml(manifest, payload)
    before = _file_state(tmp_path)

    try:
        with pytest.raises(ValueError, match="checkpoint version"):
            pipeline._load_frozen_content(checkpoint)
    finally:
        assert _file_state(tmp_path) == before


def test_frozen_content_without_a_manifest_is_absent_without_file_changes(tmp_path: Path):
    checkpoint, _ = _checkpoint(tmp_path, "Synthetic checkpoint.\n")
    (checkpoint / "frozen_content.yml").unlink()
    before = _file_state(tmp_path)

    assert pipeline._load_frozen_content(checkpoint) is None
    assert _file_state(tmp_path) == before
