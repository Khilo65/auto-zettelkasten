from auto_zettelkasten.mapping_text import prepare_embedding_spans
from auto_zettelkasten.notes import (
    RELATED_START_MARKER, RELATED_END_MARKER, RELATED_HEADING, semantic_note_hash,
)


def automatic():
    return f"{RELATED_START_MARKER}\n{RELATED_HEADING}\n\n- [[Unrelated invented navigation]]\n{RELATED_END_MARKER}\n"


def test_exported_links_do_not_become_new_embedding_evidence():
    base = "# A finding\n\n## Thesis\nInstitutions shape participation.\n"
    exported = base + "\n" + automatic()
    prepared = prepare_embedding_spans(exported)
    text = "\n".join(span["text"] for span in prepared["spans"])
    assert "Institutions shape participation" in text
    assert "invented navigation" not in text
    assert "Related notes" not in text
    assert semantic_note_hash(base) == semantic_note_hash(exported)


def test_authored_same_title_sections_and_fenced_examples_remain_evidence():
    text = "# Analysis\n\n" + RELATED_HEADING + "\n\nMy substantive distinction.\n\n```md\n" + automatic() + "```\n"
    prepared = prepare_embedding_spans(text)
    clean = "\n".join(span["text"] for span in prepared["spans"])
    assert "My substantive distinction" in clean
    assert "invented navigation" in clean
