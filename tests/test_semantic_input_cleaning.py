"""Synthetic representation controls; original source/ownership stays intact."""
from copy import deepcopy
from hashlib import sha256

import pytest

from auto_zettelkasten.mapping_text import CLEANING_VERSION, prepare_embedding_spans


def clean(text, ownership=()):
    result = prepare_embedding_spans(text, ownership)
    assert all(text[row["start"]:row["end"]] == row["text"] for row in result["spans"])
    return "".join(row["text"] for row in result["spans"])


def test_new_preparation_identity_does_not_reuse_previous_representation():
    assert CLEANING_VERSION == "local-map-spans-v5"
    assert prepare_embedding_spans("A substantive claim.")["coverage"]["cleaning_version"] == CLEANING_VERSION


@pytest.mark.parametrize("heading", [
    "Thesis", "Abstract", "Method and Research Design", "Evidence and Data",
    "Detailed Findings", "Plain-English Interpretation", "Strengths and Contributions",
    "Methodological Critique", "Limitations", "What This Source Can Support",
    "What This Source Cannot Support", "Key Concepts and Definitions",
])
def test_template_headings_do_not_become_semantic_words(heading):
    plain = "A claim is not supported outside the observed population.\n"
    assert clean(f"## {heading}\n" + plain) == clean(plain)
    assert clean(f"### {heading.upper()}:\n" + plain) == clean(plain)


def test_meaningful_titles_headings_and_qualified_analysis_remain():
    text = ("# Political exclusion and collective action\n"
            "## Thesis\nThe effect is not universal.\n"
            "## Limitations of causal inference\n"
            "The association does not establish causation.\n"
            "## Limitations\nOnly the stated population was studied.\n")
    value = clean(text)
    assert "Political exclusion and collective action" in value
    assert "Limitations of causal inference" in value
    assert "not universal" in value and "does not establish causation" in value
    assert "Only the stated population" in value
    assert "## Thesis" not in value and "## Limitations\n" not in value


@pytest.mark.parametrize("template", [
    "A [[{target}|bounded comparison]] preserves a qualification.\n",
    "A [bounded comparison]({target}) preserves a qualification.\n",
    'A [bounded comparison]({target} "hidden title") preserves a qualification.\n',
    "A [bounded comparison](<{target}>) preserves a qualification.\n",
])
def test_destination_changes_do_not_change_semantic_representation(template):
    first = template.format(target="Research/electoral-institutions")
    second = template.format(target="Research/quantum-optics")
    assert clean(first) == clean(second) == "A bounded comparison preserves a qualification.\n"


def test_markdown_balanced_destinations_titles_and_escapes_are_not_visible_content():
    text = 'Read [the result](https://example.test/a_(b) "hidden (title)") carefully.\n'
    assert clean(text) == "Read the result carefully.\n"
    assert clean(r"Read [the result](https://example.test/a_\(b\)) carefully.") == "Read the result carefully."


def test_reference_ids_and_definitions_are_not_semantic_but_visible_labels_are():
    text = "A [useful result][internal-key] remains.\n[internal-key]: https://hidden.test/destination\n"
    assert clean(text) == "A useful result remains.\n"


def test_visible_words_still_change_the_representation():
    assert clean("[[one|supports the claim]]") != clean("[[one|does not support the claim]]")
    assert clean("[[hidden|term]]") == "term"
    assert clean("[[displayed target]]") == "displayed target"
    assert clean("[[Research/displayed target#Section]]") == "Research/displayed target#Section"
    assert clean("[[target|]]") == "target"


def test_adjacent_link_labels_do_not_lose_their_separator():
    assert clean("[[first]] [[second]]") == "first second"
    assert clean("[first](url) [second](other)") == "first second"
    result = prepare_embedding_spans("Before [[hidden|visible]] after.")
    assert len(result["spans"]) == 3
    assert {row["embedding_group"] for row in result["spans"]} == {0}


@pytest.mark.parametrize("fence", ["```", "~~~~"])
def test_fenced_code_does_not_change_section_state_or_interpret_markdown(fence):
    text = ("## Thesis\nA substantive claim.\n" + fence + "text\n"
            "## Thesis\nReferences\n[[code-target|code-label]]\n"
            "[literal](code-url)\n" + fence + "\nA later substantive claim.\n")
    value = clean(text)
    assert "## Thesis\nReferences\n[[code-target|code-label]]" in value
    assert "[literal](code-url)" in value
    assert "A later substantive claim" in value
    assert "bibliography" not in prepare_embedding_spans(text)["coverage"]["excluded_by_kind"]


def test_inline_code_and_escaped_link_literals_are_not_interpreted():
    text = r"Use `[[literal|code]]` and `[literal](code-url)`; \[[escaped|literal]] stays quoted."
    assert clean(text) == text


def test_owner_body_declaration_cannot_restore_hidden_template_or_destination():
    text = "## Thesis\nA [[irrelevant destination|meaningful claim]] is not causal.\n"
    ownership = [{"start": 0, "end": len(text), "owner": "Author A", "section_kind": "body"}]
    before = deepcopy(ownership)
    result = prepare_embedding_spans(text, ownership)
    assert clean(text, ownership) == "A meaningful claim is not causal.\n"
    assert all(row["owner"] == "Author A" and row["section_kind"] == "body" for row in result["spans"])
    assert {row["section_kind"] for row in result["coverage"]["excluded_spans"]} >= {"template_heading", "link_destination"}
    assert ownership == before


def test_groups_do_not_merge_ownership_or_nonbody_boundaries():
    text = "First [[hidden|claim]].\nReferences\nA cited work.\nChapter 2\nSecond [[other|claim]].\n"
    split = text.index("Chapter 2")
    result = prepare_embedding_spans(text, [{"start": 0, "end": split, "owner": "A"},
                                          {"start": split, "end": len(text), "owner": "B"}])
    groups = {}
    for row in result["spans"]:
        groups.setdefault(row["embedding_group"], set()).add(row["owner"])
        assert text[row["start"]:row["end"]] == row["text"]
    assert all(len(owners) == 1 for owners in groups.values())
    assert len(groups) == 2
    assert "A cited work" not in clean(text)


def test_source_file_and_complete_citation_text_are_not_rewritten(tmp_path):
    text = "## Thesis\nA [claim](https://hidden.test).\nReferences\nOriginal citation and DOI.\n"
    source = tmp_path / "synthetic.md"
    source.write_bytes(text.encode())
    before = sha256(source.read_bytes()).hexdigest()
    result = prepare_embedding_spans(source.read_text())
    assert "https://hidden.test" not in "".join(row["text"] for row in result["spans"])
    assert "Original citation and DOI" in source.read_text()
    assert sha256(source.read_bytes()).hexdigest() == before
    assert result["coverage"]["full_text_preserved"] is True


def test_nested_image_label_does_not_expose_either_invisible_destination():
    assert clean("Read [![figure label](hidden-image)](hidden-link) now.") == "Read figure label now."


def test_ordinary_multiline_markdown_destination_is_not_semantic():
    text = 'Read [the result](\n  https://hidden.test/destination\n  "hidden title"\n) carefully.\n'
    assert clean(text) == "Read the result carefully.\n"


def test_managed_marker_literals_inside_code_do_not_hide_later_claims():
    text = "```text\n<!-- auto-zettelkasten:graph:start -->\n```\nA substantive claim.\n"
    assert "A substantive claim" in clean(text)
    assert "<!-- auto-zettelkasten:graph:start -->" in clean(text)


def test_unambiguous_publisher_citation_footer_does_not_embed_navigation():
    text = ("## Abstract\nInstitutions condition growth in two regimes.\n"
            "Citation\nSynthetic author. 2000. An article. DOI: 10.1000/synthetic\n"
            "JEL Classification\nO10 Development\nJournals\nAmerican Economic Review\n"
            "Membership\nCopyright notice\n## Limitations\nThe mechanism remains uncertain.\n")
    result = prepare_embedding_spans(text)
    value = "".join(row["text"] for row in result["spans"])
    assert "Institutions condition growth" in value
    assert "The mechanism remains uncertain" in value
    assert "Membership" not in value and "10.1000" not in value and "Review" not in value
    assert result["coverage"]["excluded_by_kind"]["publisher_navigation"] > 0
    assert all(text[row["start"]:row["end"]] == row["text"] for row in result["spans"])


def test_citation_prose_or_incomplete_publisher_signature_is_not_dropped():
    text = "Citation\nA discussion of citation practices and DOI: 10.1000/synthetic.\nThe argument remains substantive.\n"
    assert clean(text) == text
    fenced = "```text\nCitation\nDOI: 10.1000/synthetic\nJEL Classification\nJournals\n```\nA later claim.\n"
    assert clean(fenced) == fenced


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("heading,start,end", [
    ("Graph Links", "<!-- auto-zettelkasten:graph:start -->", "<!-- auto-zettelkasten:graph:end -->"),
    ("Position in the Literature", "<!-- auto-zettelkasten:literature:start -->", "<!-- auto-zettelkasten:literature:end -->"),
])
def test_owned_navigation_heading_is_not_semantic_but_personal_heading_remains(newline, heading, start, end):
    base = "## Thesis\nA qualified substantive claim.\n"
    owned = base + f"\n## {heading}\n\n{start}\n[[Generated target]]\n{end}\n"
    assert clean(owned.replace("\n", newline)).split() == clean(base.replace("\n", newline)).split()
    personal = base + f"\n## {heading}\n\nPersonal substantive comparison.\n"
    assert heading in clean(personal)
    wrong_marker = "<!-- auto-zettelkasten:literature:start -->" if "graph:" in start else "<!-- auto-zettelkasten:graph:start -->"
    assert heading in clean(base + f"\n## {heading}\n{wrong_marker}\n")


def test_actual_graph_writer_shape_and_appended_owned_blocks_are_semantically_invariant():
    from auto_zettelkasten.notes import GRAPH_START_MARKER, GRAPH_END_MARKER, _project_graph_block

    base = "# Synthetic source\n\n## Thesis\nA substantive finding with limits.\n"
    block = f"{GRAPH_START_MARKER}\n[[Generated hub]]\nNo committed typed links or canonical clusters yet.\n{GRAPH_END_MARKER}"
    written = _project_graph_block(base, block)
    assert "## Graph Links\n\n" + block in written
    assert clean(written).split() == clean(base).split()
    appended = (written + "\n## Graph Links\n" + block + "\n"
                "## Position in the Literature\n<!-- auto-zettelkasten:literature:start -->\n"
                "- [[Generated source]]\n<!-- auto-zettelkasten:literature:end -->\n")
    assert clean(appended).split() == clean(base).split()
    result = prepare_embedding_spans(appended)
    assert all(appended[row["start"]:row["end"]] == row["text"] for row in result["spans"])
    assert not any(row["text"].strip() in {"## Graph Links", "## Position in the Literature"} for row in result["spans"])
