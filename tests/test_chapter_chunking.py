import pytest

from auto_zettelkasten import pipeline
from auto_zettelkasten.files import read_yaml
from auto_zettelkasten.models import MapRequest, ProcessingPolicy


def _book(chapters=4, page_size=90):
    pages = []
    for page in range(1, chapters * 2 + 1):
        prefix = f"--- Page {page} ---\n"
        if page % 2:
            prefix += f"{page * 10}\n{(page + 1) // 2}\nChapter Title\nBody "
        else:
            prefix += "Continued body "
        pages.append(prefix + "x" * (page_size - len(prefix)))
    return "\n\n".join(pages)


def _bodies(text, cap, *, max_chunks=0):
    return [chunk.partition("\n")[2] for chunk in pipeline._split_document(
        text, chunk_char_limit=cap, max_chunks=max_chunks
    )]


def test_chapter_alignment_preserves_count_cap_and_source():
    text = _book()
    baseline = pipeline._pack_document_blocks(text.split("\n\n"), 500)
    chunks = _bodies(text, 500)
    assert len(chunks) == len(baseline) == 2
    assert baseline[1].startswith("--- Page 6 ---")
    assert chunks[1].startswith("--- Page 5 ---\n50\n3\nChapter Title")
    assert all(len(chunk) <= 500 for chunk in chunks)
    assert "\n\n".join(chunks) == text


@pytest.mark.parametrize("text", [
    "",
    "ordinary text\n\n" * 30,
    "--- Page 1 ---\n10\n1\nTable Results\nSome row values",
    "--- Page 1 ---\n1. First Chapter 4\n2. Second Chapter 20",
    "--- Page 1 ---\n4——Running Title\n1\nA Heading\nbody",
    _book().replace("\n2\nChapter Title", "\nChapter Title"),
    _book().replace("Chapter Title", "Table 1 Results"),
    _book().replace("\n\n--- Page", "\n--- Page"),
    _book().replace("\n2\nChapter Title", "\n" + "9" * 5000 + "\nChapter Title"),
])
def test_unrecognized_or_noisy_layout_keeps_baseline(text):
    assert _bodies(text, 120) == pipeline._pack_document_blocks(text.split("\n\n"), 120)


def test_chapter_packing_never_adds_calls():
    text = _book(chapters=3, page_size=130)
    # Three indivisible chapters need three packets, but six pages fit into two.
    baseline = pipeline._pack_document_blocks(text.split("\n\n"), 400)
    assert len(pipeline._chapter_start_offsets(text)) == 3
    assert _bodies(text, 400) == baseline and len(baseline) == 2


def test_oversized_chapters_use_paragraphs_and_keep_maximum_policy():
    text = _book()
    chunks = _bodies(text, 150)
    assert len(chunks) == 8
    assert all(len(chunk) <= 150 for chunk in chunks)
    assert "\n\n".join(chunks) == text
    with pytest.raises(pipeline.DocumentCoverageLimitError) as caught:
        _bodies(text, 500, max_chunks=1)
    assert (caught.value.total_chunks, caught.value.maximum_chunks) == (2, 1)
    assert len(_bodies(text, 500, max_chunks=2)) == 2
    # Preserve existing behavior when arbitrary slices of a long paragraph do not reconstruct.
    long_pages = _book(page_size=300)
    assert _bodies(long_pages, 150) == pipeline._pack_document_blocks(long_pages.split("\n\n"), 150)


def test_table_lookalikes_remain_only_safe_partition_hints():
    text = _book().replace("Chapter Title", "Survey Results")
    assert len(pipeline._chapter_start_offsets(text)) == 4
    chunks = _bodies(text, 500)
    assert len(chunks) == 2 and "\n\n".join(chunks) == text


def test_chunking_version_invalidates_same_count_synthesis(tmp_path, monkeypatch):
    class Reader:
        name = "fake"
        model = "fake"
        calls = 0

        def read_source(self, text, metadata, question):
            self.calls += 1
            return {"detailed_findings": "Source finding"}

        def synthesize_document(self, analyses, metadata, question, **kwargs):
            self.calls += 1
            return {"detailed_findings": "Combined findings"}

    reader = Reader()
    request = MapRequest(tmp_path, processing=ProcessingPolicy(
        direct_read_char_limit=1, chunk_char_limit=500, max_total_chunks=0,
        max_calls_per_document_run=0,
    ))
    kwargs = dict(request=request, checkpoint_root=tmp_path / "checkpoint")
    assert pipeline.CHUNKING_VERSION == "3"
    with monkeypatch.context() as previous:
        previous.setattr(pipeline, "CHUNKING_VERSION", "2")
        pipeline._read_document(reader, _book(), {}, None, **kwargs)
    assert reader.calls == 3
    pipeline._read_document(reader, _book(), {}, None, **kwargs)
    assert reader.calls == 6
    checkpoint = read_yaml(tmp_path / "checkpoint/synthesis.yml", {})
    assert checkpoint["identity"]["chunking_version"] == "3"
    assert checkpoint["identity"]["total_chunks"] == 2
    pipeline._read_document(reader, _book(), {}, None, **kwargs)
    assert reader.calls == 6
