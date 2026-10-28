import pytest

from auto_zettelkasten.zotero_links import item_library_scope, parse_zotero_uri, uri_for_item

PDF = "zotero://open-pdf/library/items/PDFKEY01"


def test_source_identity_and_physical_page_coordinates():
    parsed = parse_zotero_uri(f"{PDF}?annotation=ANNOT001&page=1")
    assert parsed is not None
    assert (parsed.action, parsed.scope, parsed.library_type, parsed.library_id, parsed.key) == (
        "open-pdf", "library", "user", None, "PDFKEY01")
    assert (parsed.page, parsed.page_index, parsed.annotation_key) == (1, 0, "ANNOT001")
    assert parsed.uri == f"{PDF}?page=1&annotation=ANNOT001"
    group = parse_zotero_uri("zotero://select/groups/42/items/PDFKEY01")
    assert group is not None and group.library_id == "42" and group.library_type == "group"
    assert group.scope != parsed.scope
    assert parse_zotero_uri("ZOTERO://select/library/items/SOURCE01").uri == "zotero://select/library/items/SOURCE01"


@pytest.mark.parametrize("value", [
    None, 1, "javascript:alert(1)", "https://example.invalid", "zotero://debug/library/items/PDFKEY01",
    f"{PDF}/", f"{PDF}#page=2", f"{PDF}?", f"{PDF}?page=0", f"{PDF}?page=-1", f"{PDF}?page=1.5",
    f"{PDF}?page=01", f"{PDF}?page=9007199254740992", f"{PDF}?page=2&page=3", f"{PDF}?annotation=short",
    f"{PDF}?annotation=annot001", f"{PDF}?page=%32", f"{PDF}?page=2&unknown=3", f"{PDF}?page=2&",
    f"{PDF}?page=2\n", f"{PDF}?page=2=3", "zotero://select/library/items/SOURCE01?page=1",
    "zotero://open-pdf/groups/0/items/PDFKEY01", "zotero://open-pdf/groups/01/items/PDFKEY01",
    "zotero://open-pdf/library/items/pdfkey01", f" {PDF}", "zotero://open-pdf@evil/library/items/PDFKEY01",
])
def test_reject_unsupported_or_ambiguous_uri(value):
    assert parse_zotero_uri(value) is None


def test_uri_generation_requires_library_scope_and_rejects_invalid_explicit_metadata():
    item = {"key": "SOURCE01", "library": {"type": "group", "id": 42}, "data": {"title": "Ignored"}}
    assert uri_for_item(item) == "zotero://select/groups/42/items/SOURCE01"
    assert uri_for_item(item, "open-pdf", key="PDFKEY01") == "zotero://open-pdf/groups/42/items/PDFKEY01"
    assert uri_for_item({"key": "SOURCE01"}) is None
    assert uri_for_item({"key": "SOURCE01"}, fallback_scope="library") == "zotero://select/library/items/SOURCE01"
    assert item_library_scope({"libraryID": 42}) is None  # internal DB IDs are not group IDs
    for library in [None, {}, {"type": "group"}, {"type": "group", "id": 0}, {"type": "unknown", "id": 2}]:
        assert uri_for_item({"key": "SOURCE01", "library": library}, fallback_scope="library") is None
