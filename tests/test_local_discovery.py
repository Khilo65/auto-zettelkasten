from __future__ import annotations

from copy import deepcopy
import json
import hashlib
from pathlib import Path
import sqlite3

import pytest

from auto_zettelkasten.discovery import (
    BASELINE_PARSER_VERSION,
    CACHE_RELATIVE_PATH,
    DiscoveryCache,
    ReferenceMatcher,
    baseline_references,
    discover_signals,
    load_cached_records,
    scan_document,
)


def identity(key: str = "ITEM0001", *, instance: str = "synthetic-desktop", library: str = "0") -> dict:
    return {"instance_id": instance, "library_type": "user", "library_id": library, "item_key": key}


def item(key: str = "ITEM0001", **fields: object) -> dict:
    return {"key": key, "data": {"key": key, "title": "Institutions and peace", "itemType": "journalArticle",
            "creators": [{"firstName": "Ada", "lastName": "Smith", "creatorType": "author"}],
            "date": "2020-03-04", "tags": [{"tag": "Shared topic"}], "collections": ["COLL0001"], **fields}}


def entry(key: str = "ITEM0002", *, instance: str = "synthetic-desktop", library: str = "0", **fields: object) -> dict:
    return {"identity": identity(key, instance=instance, library=library), "item": item(key, **fields)}


def coverage(**fields: object) -> dict:
    return {"status": "succeeded", "source_scope": "full_document", "coverage_gate": "passed", "route": "local_file",
            "indexed_pages": 10, "total_pages": 10, **fields}


def binding(key: str = "ITEM0001", source_id: str = "existing-source-one") -> dict:
    return {"source_id": source_id, "note_id": "existing-note-one", "zotero_item_key": key}


class Parser:
    def __init__(self, rows: list | None = None, *, complete: bool = True) -> None:
        self.calls = 0
        self.rows = rows or []
        self.complete = complete

    def __call__(self, text: str) -> dict:
        self.calls += 1
        return {"references": deepcopy(self.rows), "complete": self.complete}


def scan(*, key: str = "ITEM0001", parser: Parser | None = None, **kwargs: object) -> dict:
    args = {"identity": identity(key), "text": "Synthetic article body.\nReferences\nSmith 2020.", "coverage": coverage()}
    if parser is not None:
        args.update(parser=parser, parser_version="synthetic-parser-v1")
    args.update(kwargs)
    return scan_document(item(key), **args)


def test_existing_binding_and_metadata_are_preserved_without_synthesizing_ids() -> None:
    unbound = scan()
    assert unbound["source_id"] is None and unbound["note_id"] is None
    row = scan(source_binding=binding())
    assert row["source_id"] == "existing-source-one"
    assert row["note_id"] == "existing-note-one"
    assert row["metadata"]["tags"] == ["Shared topic"]
    assert row["metadata"]["collections"] == ["COLL0001"]
    assert row["metadata"]["creators"][0]["lastName"] == "Smith"
    catalogue = {"source_id": "existing-catalogue-source", "note_id": "n2",
                 "identity": {"canonical_zotero_key": "OTHER001", "zotero_item_keys": ["OTHER001", "ITEM0001"]}}
    assert scan(source_binding=catalogue)["source_id"] == "existing-catalogue-source"


@pytest.mark.parametrize("bad", [
    {"source_id": "made-up", "path": "chapter.pdf"},
    {"source_id": "wrong-item", "zotero_item_key": "OTHER001"},
    {"source_id": "wrong-instance", "identity": identity(instance="another-desktop")},
    {"source_id": "wrong-library", "identity": identity(library="2")},
])
def test_source_binding_requires_exact_identity(bad: dict) -> None:
    with pytest.raises(ValueError):
        scan(source_binding=bad)


def test_existing_zotero_key_binding_requires_unambiguous_exact_identity() -> None:
    existing = {"source_id": "existing-key-source", "note_id": "existing-key-note", "zotero_key": "ITEM0001"}
    assert scan(source_binding=existing)["source_id"] == "existing-key-source"
    with pytest.raises(ValueError, match="conflicting"):
        scan(source_binding={**existing, "zotero_item_key": "OTHER001"})
    with pytest.raises(ValueError, match="exact"):
        scan(source_binding={**existing, "zotero_key": "OTHER001"})


@pytest.mark.parametrize("bad", [
    {"library_type": "group"}, {"library_id": "local"}, {"item_key": "../file.pdf"}, {"instance_id": ""},
])
def test_unsupported_or_missing_identity_is_rejected(bad: dict) -> None:
    with pytest.raises(ValueError):
        scan(identity={**identity(), **bad})


def test_metadata_key_and_personal_library_must_agree() -> None:
    with pytest.raises(ValueError, match="item key"):
        scan_document(item("OTHER001"), identity=identity(), text="body", coverage=coverage())
    with pytest.raises(ValueError, match="library"):
        scan_document({**item(), "library": {"type": "group", "id": 0}}, identity=identity(), text="body", coverage=coverage())


def test_identifier_first_article_and_book_matching() -> None:
    matcher = ReferenceMatcher([entry(DOI="https://doi.org/10.1234/ABC"),
                                entry("BOOK0001", itemType="book", ISBN="978-0-306-40615-7", title="A book")])
    article = matcher.match({"identifiers": {"doi": "doi:10.1234/abc."}}, identity=identity())
    assert article["match_status"] == "matched_identifier"
    assert article["matched_item_keys"] == ["ITEM0002"]
    assert article["work_id"] == "doi:10.1234/abc"
    book = matcher.match({"identifiers": {"isbn": "9780306406157"}, "item_type": "book"}, identity=identity())
    assert book["match_status"] == "matched_identifier"
    assert book["work_id"] == "isbn:9780306406157"
    invalid = matcher.match({"identifiers": {"isbn": "9780306406158"}}, identity=identity())
    assert invalid["identifiers"] == {} and invalid["match_status"] == "unresolved"


def test_doi_free_exact_match_and_no_fuzzy_or_wildcard_matches() -> None:
    matcher = ReferenceMatcher([entry()])
    ref = {"title": "Institutions & peace", "author": "Smith", "year": "2020"}
    # Punctuation normalizes, but replacing a word with & cannot change its meaning.
    assert matcher.match(ref, identity=identity())["matched_item_keys"] == []
    ref["title"] = "INSTITUTIONS AND PEACE."
    assert matcher.match(ref, identity=identity())["match_status"] == "matched_title_author_year"
    for field in ("title", "author", "year"):
        incomplete = {key: value for key, value in ref.items() if key != field}
        assert matcher.match(incomplete, identity=identity())["matched_item_keys"] == []
    assert matcher.match({**ref, "author": "Jones"}, identity=identity())["matched_item_keys"] == []
    assert matcher.match({**ref, "title": "Institutions and peaceful reform"}, identity=identity())["matched_item_keys"] == []


def test_ambiguous_editions_and_conflicting_identifiers_remain_unresolved() -> None:
    matcher = ReferenceMatcher([entry("BOOK0001", itemType="book", edition="1", DOI="10.1234/one"),
                                entry("BOOK0002", itemType="book", edition="2", DOI="10.1234/two")])
    ambiguous = matcher.match({"title": "Institutions and peace", "author": "Smith", "year": "2020"}, identity=identity())
    assert ambiguous["match_status"] == "ambiguous"
    assert ambiguous["candidate_item_keys"] == ["BOOK0001", "BOOK0002"]
    assert ambiguous["matched_item_keys"] == [] and ambiguous["work_id"] is None
    conflict = matcher.match({"identifiers": {"doi": "10.1234/elsewhere"}, "title": "Institutions and peace",
                              "author": "Smith", "year": "2020"}, identity=identity())
    assert conflict["match_status"] == "identifier_conflict" and conflict["work_id"] is None


def test_matching_never_crosses_library_or_instance() -> None:
    matcher = ReferenceMatcher([entry(DOI="10.1234/abc", library="5"),
                                entry("ITEM0003", DOI="10.1234/abc", instance="other-desktop")])
    ref = matcher.match({"identifiers": {"doi": "10.1234/abc"}}, identity=identity())
    assert ref["match_status"] == "external" and ref["matched_item_keys"] == []


def test_chapter_cannot_match_book_on_container_isbn() -> None:
    matcher = ReferenceMatcher([entry("BOOK0001", itemType="book", ISBN="9780306406157"),
                                entry("CHAP0001", itemType="bookSection", title="One chapter", ISBN="9780306406157")])
    unknown = matcher.match({"item_type": "bookSection", "identifiers": {"isbn": "9780306406157"}}, identity=identity())
    assert unknown["match_status"] == "external" and unknown["work_id"] is None
    chapter = matcher.match({"item_type": "bookSection", "identifiers": {"isbn": "9780306406157"},
                             "title": "One chapter", "author": "Smith", "year": "2020"}, identity=identity())
    assert chapter["matched_item_keys"] == ["CHAP0001"]
    book_only = ReferenceMatcher([entry("BOOK0001", itemType="book", ISBN="9780306406157")])
    assert book_only.match({"identifiers": {"isbn": "9780306406157"}, "item_type": "bookSection"}, identity=identity())["matched_item_keys"] == []


def test_baseline_keeps_chapter_bibliographies_and_unparsed_unknowns() -> None:
    text = "Chapter 1\nBody must stay private.\nReferences\nSmith. A DOI-free book.\nChapter 2\nAnother body.\nBibliography\nJones. Article doi:10.1234/xyz.\nAppendix A\nNot a reference."
    row = scan(text=text)
    assert row["parser_version"] == BASELINE_PARSER_VERSION
    assert len(row["references"]) == 2
    assert row["references"][0]["title"] is None
    assert row["references"][0]["author"] is None
    assert row["references"][0]["year"] is None
    assert row["references"][0]["match_status"] == "unresolved"
    assert row["references"][1]["work_id"] == "doi:10.1234/xyz"
    assert row["references"][1]["locator"] == {"line": 8}
    assert row["bibliography"]["status"] == "unknown"
    assert "Body must stay private" not in json.dumps(row)
    assert baseline_references("No bibliography here")["complete"] is False


def test_parser_retains_chapter_locator_and_complete_empty_requires_evidence() -> None:
    parser = Parser([{"title": "Chapter study", "author": "Jones", "year": "1999", "item_type": "bookSection",
                      "locator": {"page": 45, "chapter": "2"}}])
    row = scan(parser=parser)
    assert row["references"][0]["locator"] == {"page": 45, "chapter": "2"}
    assert row["references"][0]["work_id"] is None
    assert row["bibliography"]["status"] == "complete"
    assert scan(parser=Parser())["bibliography"]["status"] == "complete"
    assert scan(text="Readable full text without an explicit bibliography")["bibliography"]["status"] == "unknown"


@pytest.mark.parametrize("limited", [
    coverage(source_scope="partial_document"), coverage(indexed_pages=2, total_pages=100),
    coverage(indexed_pages=None, total_pages=100), coverage(coverage_gate="limited"),
    coverage(status="partial"), {key: value for key, value in coverage().items() if key != "coverage_gate"},
    coverage(indexedChars=50, totalChars=100),
])
def test_partial_or_unknown_coverage_cannot_reuse_cache_or_prove_empty(tmp_path: Path, limited: dict) -> None:
    parser = Parser()
    cache = DiscoveryCache(tmp_path / CACHE_RELATIVE_PATH)
    first = scan(parser=parser, cache=cache, coverage=limited)
    assert first["coverage"]["sufficient"] is False
    assert first["bibliography"]["status"] == "unknown"
    assert first["references"] == []
    assert cache.get(identity(), first["input_fingerprint"]) is None
    scan(parser=parser, cache=cache, coverage=limited)
    assert parser.calls == 2


@pytest.mark.parametrize("status,scope,text", [
    ("scanned", "full_document", ""), ("failed", "full_document", ""),
    ("succeeded", "metadata_only", "An abstract"), ("succeeded", "abstract_only", "References\nBody"),
    ("succeeded", "full_document", ""),
])
def test_scanned_failed_and_metadata_only_remain_unknown(status: str, scope: str, text: str) -> None:
    parser = Parser()
    row = scan(parser=parser, text=text, coverage=coverage(status=status, source_scope=scope))
    assert parser.calls == 0
    assert row["bibliography"]["status"] == "unknown"
    assert row["coverage"]["sufficient"] is False
    assert row["references"] == []


def test_full_cache_reuses_only_identical_inputs_and_keeps_no_body(tmp_path: Path) -> None:
    parser = Parser([{"identifiers": {"doi": "10.1234/external"}}])
    path = tmp_path / CACHE_RELATIVE_PATH
    cache = DiscoveryCache(path)
    body = "UNIQUE SYNTHETIC FULL BODY MUST NEVER BE STORED."
    first = scan(parser=parser, cache=cache, text=body)
    assert scan(parser=parser, cache=cache, text=body) == first
    assert parser.calls == 1
    assert list(path.parent.iterdir()) == [path]
    assert body.encode() not in path.read_bytes()
    assert cache.get(identity(instance="other"), first["input_fingerprint"]) is None


@pytest.mark.parametrize("change", ["metadata", "text", "coverage", "policy", "parser", "library", "binding"])
def test_cache_invalidates_all_semantic_inputs(tmp_path: Path, change: str) -> None:
    parser = Parser()
    args = {"identity": identity(), "text": "Synthetic body.", "coverage": coverage(), "parser": parser,
            "parser_version": "fixture-v1", "source_binding": binding(), "cache": DiscoveryCache(tmp_path / CACHE_RELATIVE_PATH)}
    metadata = item()
    first = scan_document(metadata, **args)
    if change == "metadata":
        metadata["data"]["tags"].append({"tag": "new"})
    elif change == "text":
        args["text"] += " Changed."
    elif change == "coverage":
        args["coverage"] = coverage(route="zotero_fulltext")
    elif change == "policy":
        args["extraction_policy"] = "fixture-policy-v2"
    elif change == "parser":
        args["parser_version"] = "fixture-v2"
    elif change == "library":
        args["library_items"] = [entry()]
    elif change == "binding":
        args["source_binding"] = binding(source_id="existing-source-two")
    second = scan_document(metadata, **args)
    assert first["input_fingerprint"] != second["input_fingerprint"]
    assert parser.calls == 2


def test_partial_cache_does_not_mask_new_full_extraction(tmp_path: Path) -> None:
    cache = DiscoveryCache(tmp_path / CACHE_RELATIVE_PATH)
    partial = scan(parser=Parser(), coverage=coverage(indexed_pages=1, total_pages=10), cache=cache)
    parser = Parser([{"identifiers": {"doi": "10.1234/new"}}])
    full = scan(parser=parser, cache=cache)
    assert full["input_fingerprint"] != partial["input_fingerprint"]
    assert full["bibliography"]["status"] == "complete" and len(full["references"]) == 1
    assert parser.calls == 1


def test_shared_external_reference_tag_related_and_direct_signals_stay_distinct() -> None:
    external = {"identifiers": {"doi": "10.1234/outside"}}
    citing = scan_document(item(relations={"dc:relation": ["http://zotero.org/users/0/items/ITEM0002"],
                                           "dc:replaces": ["ITEM0002"]}), identity=identity(), text="body",
                           coverage=coverage(), library_items=[entry(DOI="10.1234/internal")],
                           parser=Parser([external, {"identifiers": {"doi": "10.1234/internal"}}]), parser_version="fixture-v1")
    target = scan(key="ITEM0002", parser=Parser([external]))
    signals = discover_signals([citing, target])["signals"]
    assert {row["kind"] for row in signals} == {"direct_citation", "shared_reference", "shared_tag", "zotero_related"}
    shared = [row for row in signals if row["kind"] == "shared_reference"]
    assert len(shared) == 1 and shared[0]["evidence"] == "doi:10.1234/outside"
    assert all("accepted" not in row and "relation_type" not in row for row in signals)
    assert not any(row["evidence"] == "dc:replaces" for row in signals)


def test_doi_free_external_article_identity_and_unknown_book_editions() -> None:
    article = {"title": "An external study", "author": "Jones", "year": "2001", "item_type": "journalArticle"}
    matcher = ReferenceMatcher()
    assert matcher.match(article, identity=identity())["work_id"].startswith("title:")
    assert matcher.match({**article, "item_type": "book"}, identity=identity())["work_id"] is None
    assert matcher.match({**article, "item_type": "bookSection"}, identity=identity())["work_id"] is None
    assert matcher.match({key: value for key, value in article.items() if key != "item_type"}, identity=identity())["work_id"] is None


def test_related_uri_namespace_and_shared_signals_respect_scope() -> None:
    relations = {"dc:relation": ["http://zotero.org/groups/0/items/ITEM0002", "http://zotero.org/users/9/items/ITEM0002",
                                  "https://elsewhere.test/users/0/items/ITEM0002"]}
    source = scan_document(item(relations=relations), identity=identity(), text="body", coverage=coverage())
    other = scan(key="ITEM0002")
    assert {row["kind"] for row in discover_signals([source, other])["signals"]} == {"shared_tag"}
    other["identity"]["instance_id"] = "other"
    assert discover_signals([source, other])["signals"] == []


def test_signal_bounds_skip_popular_groups_and_cap_output() -> None:
    records = [scan(key=f"ITEM{i:04d}", parser=Parser([{"identifiers": {"doi": "10.1234/common"}}])) for i in range(4)]
    bounded = discover_signals(records, max_group_size=3)
    assert bounded["signals"] == [] and bounded["skipped_groups"] == 2 and bounded["truncated"]
    capped = discover_signals(records, max_signals=2)
    assert len(capped["signals"]) == 2 and capped["truncated"]
    assert discover_signals(records[:2])["truncated"] is False


@pytest.mark.parametrize("bad_result", [None, {}, {"references": [], "complete": "yes"},
                                        {"references": [{"identifiers": []}], "complete": True},
                                        {"references": [{}] * 2001, "complete": True}])
def test_corrupt_or_oversized_parser_output_stays_failed_unknown(bad_result: object) -> None:
    row = scan(parser=lambda text: bad_result)
    assert row["bibliography"] == {"status": "failed", "reason": "local_parser_failed"}
    assert row["references"] == []


def test_failed_parser_cache_is_retryable(tmp_path: Path) -> None:
    calls = []

    def parser(text: str) -> dict:
        calls.append(text)
        raise RuntimeError("synthetic adapter failure")

    cache = DiscoveryCache(tmp_path / CACHE_RELATIVE_PATH)
    assert scan(parser=parser, cache=cache)["bibliography"]["status"] == "failed"
    scan(parser=parser, cache=cache)
    assert len(calls) == 2


@pytest.mark.parametrize("bad", [b"binary text", "bad\x00text", "x" * 8_000_001])
def test_corrupt_or_oversized_text_is_rejected(bad: object) -> None:
    with pytest.raises(ValueError):
        scan(text=bad)


def test_metadata_and_cache_schema_limits(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        scan_document(item(abstractNote="x" * 262_145), identity=identity(), text="body", coverage=coverage())
    with pytest.raises(ValueError):
        scan(coverage=coverage(indexed_pages=-1))
    with pytest.raises(ValueError):
        scan(parser=Parser(), parser_version=BASELINE_PARSER_VERSION)
    with pytest.raises(ValueError):
        DiscoveryCache(tmp_path / "visible-cache.sqlite3")
    cache = DiscoveryCache(tmp_path / CACHE_RELATIVE_PATH)
    record = scan()
    with pytest.raises(ValueError, match="fields"):
        cache.put({**record, "text": "Must not persist body text"})


def test_corrupt_or_oversized_cached_record_is_a_miss(tmp_path: Path) -> None:
    cache = DiscoveryCache(tmp_path / CACHE_RELATIVE_PATH)
    record = scan(parser=Parser(), cache=cache)
    for corrupt in ("{bad-json", json.dumps({"identity": identity()}), "x" * 2_000_001):
        with sqlite3.connect(cache.path) as connection:
            connection.execute("UPDATE documents SET record = ?", (corrupt,))
        assert cache.get(identity(), record["input_fingerprint"]) is None


def test_corrupt_database_is_preserved_and_reports_error(tmp_path: Path) -> None:
    path = tmp_path / CACHE_RELATIVE_PATH
    path.parent.mkdir()
    path.write_bytes(b"synthetic corrupt database")
    with pytest.raises(sqlite3.DatabaseError):
        DiscoveryCache(path)
    assert path.read_bytes() == b"synthetic corrupt database"


def test_read_only_loader_does_not_create_missing_cache(tmp_path: Path) -> None:
    result = load_cached_records(tmp_path / CACHE_RELATIVE_PATH, instance_id="synthetic-desktop", library_id="0")
    assert result == {"records": [], "freshness": "cached_unverified", "truncated": False, "skipped_records": 0}
    assert list(tmp_path.iterdir()) == []


def test_loader_scopes_parser_and_latest_item_without_claiming_freshness(tmp_path: Path) -> None:
    cache = DiscoveryCache(tmp_path / CACHE_RELATIVE_PATH)
    first = scan(cache=cache, text="References\nFirst reference")
    latest = scan(cache=cache, text="References\nLater reference")
    assert latest["text_hash"] == hashlib.sha256(b"References\nLater reference").hexdigest()
    assert latest["metadata_fingerprint"] == first["metadata_fingerprint"]
    scan(key="ITEM0002", cache=cache, identity=identity("ITEM0002", instance="other-desktop"))
    scan(key="ITEM0003", cache=cache, identity=identity("ITEM0003", library="9"))
    scan(key="ITEM0004", cache=cache, parser=Parser())
    before = cache.path.read_bytes()
    loaded = load_cached_records(cache.path, instance_id="synthetic-desktop", library_id="0")
    assert loaded["records"] == [latest]
    assert loaded["freshness"] == "cached_unverified"
    assert loaded["records"][0]["bibliography"]["status"] == "unknown"
    assert cache.path.read_bytes() == before
    fixture = load_cached_records(cache.path, instance_id="synthetic-desktop", library_id="0", parser_version="synthetic-parser-v1")
    assert [row["identity"]["item_key"] for row in fixture["records"]] == ["ITEM0004"]


def test_loader_preserves_partial_records_and_bounds_rows_and_bytes(tmp_path: Path) -> None:
    cache = DiscoveryCache(tmp_path / CACHE_RELATIVE_PATH)
    scan(key="ITEM0001", cache=cache, coverage=coverage(source_scope="partial_document"))
    scan(key="ITEM0002", cache=cache)
    loaded = load_cached_records(cache.path, instance_id="synthetic-desktop", library_id="0", limit=1)
    assert len(loaded["records"]) == 1 and loaded["truncated"]
    assert loaded["records"][0]["coverage"]["sufficient"] is False
    assert loaded["records"][0]["bibliography"]["status"] == "unknown"
    tiny = load_cached_records(cache.path, instance_id="synthetic-desktop", library_id="0", max_bytes=1)
    assert tiny["records"] == [] and tiny["truncated"]


def test_loader_skips_corruption_and_column_identity_mismatch(tmp_path: Path) -> None:
    cache = DiscoveryCache(tmp_path / CACHE_RELATIVE_PATH)
    scan(cache=cache)
    with sqlite3.connect(cache.path) as connection:
        connection.execute("UPDATE documents SET item_key = ?", ("OTHER001",))
    loaded = load_cached_records(cache.path, instance_id="synthetic-desktop", library_id="0")
    assert loaded["records"] == [] and loaded["skipped_records"] == 1
    with sqlite3.connect(cache.path) as connection:
        connection.execute("UPDATE documents SET record = ?", ("{corrupt-json",))
    assert load_cached_records(cache.path, instance_id="synthetic-desktop", library_id="0")["skipped_records"] == 1


@pytest.mark.parametrize("field", ["identifiers", "creator", "locator", "reference_identifiers"])
def test_cache_rejects_hidden_full_text_in_nested_fields(tmp_path: Path, field: str) -> None:
    cache = DiscoveryCache(tmp_path / CACHE_RELATIVE_PATH)
    row = scan(parser=Parser([{"identifiers": {"doi": "10.1234/example"}}]))
    if field == "identifiers":
        target = row["metadata"]["identifiers"]
    elif field == "creator":
        target = row["metadata"]["creators"][0]
    elif field == "locator":
        target = row["references"][0]["locator"]
    else:
        target = row["references"][0]["identifiers"]
    target["full_text"] = "Private body text must not be cached."
    with pytest.raises(ValueError):
        cache.put(row)


def test_cache_rejects_contradictory_coverage_and_bibliography(tmp_path: Path) -> None:
    cache = DiscoveryCache(tmp_path / CACHE_RELATIVE_PATH)
    row = scan(coverage=coverage(status="failed", source_scope="metadata_only"))
    row["coverage"]["sufficient"] = True
    with pytest.raises(ValueError, match="contradictory"):
        cache.put(row)
    row = scan(coverage=coverage(source_scope="partial_document"))
    row["bibliography"]["status"] = "complete"
    with pytest.raises(ValueError, match="contradictory"):
        cache.put(row)


def test_source_binding_cannot_hide_partial_mismatching_scope() -> None:
    for fields in ({"library_type": "group"}, {"library_id": "999"}, {"instance_id": "other"}):
        with pytest.raises(ValueError, match="crosses"):
            scan(source_binding={"source_id": "existing-id", "identity": {"item_key": "ITEM0001", **fields}})


@pytest.mark.parametrize("contradiction", [{"title": "An unrelated study"}, {"author": "Jones"}, {"year": "1999"}])
def test_known_contradictions_prevent_confirmed_doi_match(contradiction: dict) -> None:
    matcher = ReferenceMatcher([entry(DOI="10.1234/abc")])
    reference = matcher.match({"identifiers": {"doi": "10.1234/abc"}, **contradiction}, identity=identity())
    assert reference["match_status"] == "identifier_conflict"
    assert reference["matched_item_keys"] == [] and reference["work_id"] is None
    assert matcher.match({"identifiers": {"doi": "10.1234/abc"}}, identity=identity())["match_status"] == "matched_identifier"


def test_doi_and_doi_free_references_to_same_item_share_work_identity() -> None:
    matcher = ReferenceMatcher([entry("WORK0001", DOI="10.1234/library")])
    first = scan(matcher=matcher, parser=Parser([{"identifiers": {"doi": "10.1234/library"}}]))
    second = scan(key="ITEM0002", matcher=matcher, parser=Parser([{"title": "Institutions and peace", "author": "Smith", "year": "2020"}]))
    shared = [row for row in discover_signals([first, second])["signals"] if row["kind"] == "shared_reference"]
    assert len(shared) == 1 and shared[0]["evidence"] == "doi:10.1234/library"


def test_identifier_punctuation_and_equivalent_isbn_formats() -> None:
    matcher = ReferenceMatcher([entry(DOI="10.1234/article"),
                                entry("BOOK0001", itemType="book", ISBN="9780306406157")])
    assert matcher.match({"identifiers": {"doi": "https://doi.org/10.1234/article)."}}, identity=identity())["matched_item_keys"] == ["ITEM0002"]
    assert matcher.match({"identifiers": {"isbn": "0-306-40615-2"}, "item_type": "book"}, identity=identity())["matched_item_keys"] == ["BOOK0001"]
    assert ReferenceMatcher().match({"identifiers": {"doi": "10.1234/valid(parentheses)"}}, identity=identity())["work_id"] == "doi:10.1234/valid(parentheses)"


def test_deeply_nested_cached_json_is_a_miss_and_skipped(tmp_path: Path) -> None:
    cache = DiscoveryCache(tmp_path / CACHE_RELATIVE_PATH)
    record = scan(cache=cache)
    with sqlite3.connect(cache.path) as connection:
        connection.execute("UPDATE documents SET record = ?", ("[" * 10_000 + "0" + "]" * 10_000,))
    assert cache.get(identity(), record["input_fingerprint"]) is None
    loaded = load_cached_records(cache.path, instance_id="synthetic-desktop", library_id="0")
    assert loaded["records"] == [] and loaded["skipped_records"] == 1
