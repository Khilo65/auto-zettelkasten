from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from auto_zettelkasten.extraction import ContentAdequacy, ContentAdequacyClass, ExtractionResult
from auto_zettelkasten.files import read_yaml, write_yaml
from auto_zettelkasten.local_tools import CACHE_RELATIVE_PATH, load_local_retrieval, scan_local_collection
from auto_zettelkasten.models import EvidenceProfile
from auto_zettelkasten.profiles import profile_sidecar_path, save_profile
from auto_zettelkasten.relationships import profile_content_hash


TEXT = "References\nSmith (2020). A synthetic example. doi:10.1000/example.\n"


def _item(key: str, **fields: Any) -> dict[str, Any]:
    return {"key": key, "data": {"key": key, "itemType": "journalArticle", "title": f"Synthetic {key}", **fields}}


class LocalClient:
    library_id = "0"

    def __init__(self, items: list[dict[str, Any]], *, partial: bool = False, document: bytes | None = None):
        self.items = items
        self.partial = partial
        self.document = document
        self.calls: list[tuple[Any, ...]] = []

    def item(self, key, *, max_bytes):
        assert max_bytes == 262_144
        self.calls.append(("item", key))
        return next((item for item in self.items if item["key"] == key), None)

    def inventory_page(self, scope, collection_key, *, start, limit):
        self.calls.append(("page", scope, collection_key, start, limit))
        return {"records": self.items[start:start + limit], "start": start, "limit": limit,
                "next_start": start + limit if start + limit < len(self.items) else None, "total": len(self.items)}

    def children(self, key, *, limit, max_bytes):
        assert limit == 64 and max_bytes == 2 * 1024 * 1024
        self.calls.append(("children", key))
        return [{"key": f"{key}PDF", "data": {"key": f"{key}PDF", "itemType": "attachment", "contentType": "application/pdf", "filename": "synthetic.pdf", "linkMode": "imported_file"}},
                {"key": "IGNORED", "data": {"itemType": "note"}},
                {"key": "LINK", "data": {"itemType": "attachment", "linkMode": "linked_url"}}]

    def fulltext(self, key, *, max_bytes):
        self.calls.append(("fulltext", key, max_bytes))
        return {"content": TEXT, "indexedPages": 1, "totalPages": 3 if self.partial else 1}

    def file(self, key, *, max_bytes):
        self.calls.append(("file", key, max_bytes))
        return (self.document, "application/pdf") if self.document is not None else None


def _workspace(root: Path, count: int = 2) -> list[EvidenceProfile]:
    sources, profiles = [], []
    for key in "AB"[:count]:
        sid, nid = f"source-{key.lower()}", f"note-{key.lower()}"
        profile = EvidenceProfile(source_id=sid, note_id=nid, context={"thesis": "Institutions and synthetic mediation mechanisms"},
                                  methods=["Comparative case analysis"], concepts=["synthetic mediation"], source_role="empirical_study")
        save_profile(root / "02_source_memory/profiles", profile)
        sources.append({"source_id": sid, "note_id": nid, "zotero_key": key, "title": f"Synthetic {key}",
                        "author": "Smith", "year": "2020", "collection_keys": [key], "source_scope": "full_document",
                        "evidence_eligibility": "substantive_bounded", "thesis": "Institutions and mediation"})
        write_yaml(root / "11_state/note_metadata" / f"{nid}.yml", {"note_path": f"notes/{nid}.md", "frontmatter": {
            "source_id": sid, "note_id": nid, "source_pdf_uri": f"zotero://open-pdf/library/items/{key}PDF?page=12"}})
        profiles.append(profile)
    write_yaml(root / "02_source_memory/indexes/source_catalogue.yml", {"sources": sources})
    write_yaml(root / "02_source_memory/indexes/typed_links.yml", {"relations": [
        {"source_id": "source-a", "target_source_id": "source-b", "decision_status": "accepted", "active": True,
         "relation_id": "synthetic-link", "relation_type": "complements", "reason": "Compare mechanisms."},
        {"source_id": "source-a", "target_source_id": "source-b", "decision_status": "no_relationship", "active": True}]})
    return profiles


def _hashes(root: Path) -> dict[str, str]:
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in root.rglob("*") if path.is_file() and CACHE_RELATIVE_PATH.parent not in path.relative_to(root).parents}


def test_explicit_scan_uses_one_collection_page_and_writes_only_cache(tmp_path: Path) -> None:
    _workspace(tmp_path)
    before = _hashes(tmp_path)
    client = LocalClient([_item("A"), _item("B")])
    result = scan_local_collection(tmp_path, instance_id="desktop-one", collection_key="C", start=1, limit=1, client=client)
    assert client.calls[0] == ("page", "collection", "C", 1, 1)
    assert sum(call[0] == "page" for call in client.calls) == 1
    assert not any(call[0] == "file" for call in client.calls)
    assert result["documents"][0]["source_id"] == "source-b"
    assert result["documents"][0]["coverage"]["sufficient"] is True
    assert result["page"]["next_start"] is None
    assert (tmp_path / CACHE_RELATIVE_PATH).is_file()
    assert _hashes(tmp_path) == before


@pytest.mark.parametrize("options", [{}, {"item_keys": ["A"], "collection_key": "C"}, {"item_keys": ["A"], "start": 1},
                                     {"collection_key": "C", "limit": 65}, {"collection_key": "C", "start": -1}, {"item_keys": ["A"] * 65}])
def test_scan_rejects_unbounded_or_ambiguous_request_before_acquisition(tmp_path: Path, options: dict[str, Any]) -> None:
    client = LocalClient([])
    with pytest.raises(ValueError):
        scan_local_collection(tmp_path, instance_id="desktop-one", client=client, **options)
    assert client.calls == []
    assert not (tmp_path / CACHE_RELATIVE_PATH).exists()


def test_partial_pdf_cache_falls_through_local_file_without_ocr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def extract(raw, *, media_type, filename, ocr_mode):
        calls.append((raw, media_type, ocr_mode))
        return ExtractionResult("succeeded", text=TEXT, route="pdf_text", reason="synthetic_pdf", page_count=3,
                                adequacy=ContentAdequacy(ContentAdequacyClass.FULL_PDF_TEXT, "full_document", "passed", "synthetic_pdf"))

    monkeypatch.setattr("auto_zettelkasten.local_tools.extract_bytes", extract)
    client = LocalClient([_item("A")], partial=True, document=b"synthetic pdf bytes")
    result = scan_local_collection(tmp_path, instance_id="desktop-one", item_keys=["A"], client=client)
    record = result["documents"][0]
    assert calls == [(b"synthetic pdf bytes", "application/pdf", "off")]
    assert record["coverage"]["sufficient"] is True
    assert record["coverage"]["total_pages"] == 3
    assert record["source_id"] is None and record["note_id"] is None


def test_unreadable_or_missing_document_is_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("auto_zettelkasten.local_tools.extract_bytes", lambda *_args, **_kwargs: ExtractionResult("failed", reason="scanned_pdf_without_text"))
    client = LocalClient([_item("A"), _item("B")], partial=True, document=b"scanned fixture")
    client.fulltext = lambda *_args, **_kwargs: None
    records = scan_local_collection(tmp_path, instance_id="desktop-one", item_keys=["A"], client=client)["documents"]
    assert records[0]["coverage"]["status"] == "failed"
    assert records[0]["coverage"]["sufficient"] is False
    assert records[0]["bibliography"]["status"] == "unknown"
    client.document = None
    record = scan_local_collection(tmp_path, instance_id="desktop-one", item_keys=["B"], client=client)["documents"][0]
    assert record["coverage"]["status"] == "not_examined"
    assert record["bibliography"]["status"] == "unknown"


def test_oversized_attachment_never_reaches_extractor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("auto_zettelkasten.local_tools.MAX_ATTACHMENT_BYTES", 8)
    monkeypatch.setattr("auto_zettelkasten.local_tools.extract_bytes", lambda *_args, **_kwargs: pytest.fail("oversized parse"))
    client = LocalClient([_item("A")], document=b"x" * 9)
    client.fulltext = lambda *_args, **_kwargs: None
    result = scan_local_collection(tmp_path, instance_id="desktop-one", item_keys=["A"], client=client)
    assert result["documents"][0]["coverage"]["status"] == "failed"


def test_missing_cache_recall_preserves_paths_hashes_and_bounds(tmp_path: Path) -> None:
    profiles = _workspace(tmp_path)
    before = _hashes(tmp_path)
    loaded = load_local_retrieval(tmp_path)
    assert len(loaded["relationships"]) == 1
    assert loaded["rows"][0]["profile_hash"] == profile_content_hash(profiles[0])
    assert loaded["status"]["reference_coverage"] == "not_examined"
    result = loaded["index"].recall("synthetic mediation", relationships=loaded["relationships"], max_notes=1, max_chars=1800)
    assert len(result["notes"]) <= 1 and result["chars"] <= 1800
    assert result["notes"][0]["note_path"] == "notes/note-a.md"
    assert "zotero://open-pdf/" in str(result["notes"][0]["source_references"])
    assert not (tmp_path / CACHE_RELATIVE_PATH).exists()
    assert _hashes(tmp_path) == before


def test_cache_instance_isolation_and_read_only_loading(tmp_path: Path) -> None:
    _workspace(tmp_path)
    scan_local_collection(tmp_path, instance_id="desktop-one", item_keys=["A"], client=LocalClient([_item("A")]))
    cache = tmp_path / CACHE_RELATIVE_PATH
    before = cache.read_bytes()
    assert load_local_retrieval(tmp_path, instance_id="desktop-two")["status"]["cached_document_count"] == 0
    assert load_local_retrieval(tmp_path)["status"]["cached_document_count"] == 0
    loaded = load_local_retrieval(tmp_path, instance_id="desktop-one")
    assert loaded["status"]["cached_document_count"] == 1
    assert loaded["status"]["reference_coverage"] == "cached_unverified"
    assert cache.read_bytes() == before


@pytest.mark.parametrize("bounds", [{"max_files": 1}, {"max_file_bytes": 10}, {"max_total_bytes": 20}])
def test_load_reserves_all_budgets_before_yaml_parse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bounds: dict[str, int]) -> None:
    _workspace(tmp_path)
    monkeypatch.setattr("auto_zettelkasten.local_tools.yaml.parse", lambda *_args, **_kwargs: pytest.fail("parse before admission"))
    with pytest.raises(ValueError, match="limit"):
        load_local_retrieval(tmp_path, **bounds)


@pytest.mark.parametrize("text", ["sources: &loop [*loop]\n", "sources: " + "[" * 40 + "]" * 40])
def test_load_rejects_recursive_aliases_or_depth(tmp_path: Path, text: str) -> None:
    path = tmp_path / "02_source_memory/indexes/source_catalogue.yml"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    with pytest.raises(ValueError, match="aliases|depth"):
        load_local_retrieval(tmp_path)


def test_load_rejects_alias_expansion_and_accepts_normal_aliases(tmp_path: Path) -> None:
    path = tmp_path / "02_source_memory/indexes/source_catalogue.yml"
    path.parent.mkdir(parents=True)
    path.write_text("sources: &empty []\nother: *empty\n")
    assert load_local_retrieval(tmp_path)["status"]["source_count"] == 0
    path.write_text("a: &a [one]\n" + "\n".join(f"{chr(98 + i)}: &{chr(98 + i)} [" + ", ".join([f"*{chr(97 + i)}"] * 10) + "]" for i in range(7)))
    with pytest.raises(ValueError, match="expansion"):
        load_local_retrieval(tmp_path)


def test_load_never_reads_history_or_attachments(tmp_path: Path) -> None:
    _workspace(tmp_path)
    history = tmp_path / "11_state/runs/old/build_map_manifest.yml"
    history.parent.mkdir(parents=True)
    history.write_text("invalid historical input: [")
    assert load_local_retrieval(tmp_path)["status"]["source_count"] == 2


def test_cross_page_reference_uses_known_catalogue_and_cached_metadata(tmp_path: Path) -> None:
    _workspace(tmp_path)
    catalogue_path = tmp_path / "02_source_memory/indexes/source_catalogue.yml"
    catalogue = read_yaml(catalogue_path)
    catalogue["sources"][1]["identity"] = {"doi": "10.1000/example", "canonical_zotero_key": "B", "zotero_item_keys": ["B"]}
    write_yaml(catalogue_path, catalogue)
    first = scan_local_collection(tmp_path, instance_id="desktop-one", collection_key="C", limit=1,
                                  client=LocalClient([_item("A"), _item("B", DOI="10.1000/example")]))
    assert first["documents"][0]["references"][0]["matched_item_keys"] == ["B"]
    assert "outside this index is unexamined" in first["matching_scope"]
    cache = tmp_path / CACHE_RELATIVE_PATH
    original = cache.read_bytes()
    index = load_local_retrieval(tmp_path, instance_id="desktop-one")["index"]
    pairs = index.candidate_pairs(max_pairs=1)
    assert any(signal["signal"] == "direct_reference" for signal in pairs[0]["discovery_provenance"])
    assert cache.read_bytes() == original


@pytest.mark.parametrize("pending,changed", [(True, False), (False, True), (False, False)])
def test_authoritative_current_memory_controls_neighbor_freshness(tmp_path: Path, pending: bool, changed: bool) -> None:
    profiles = _workspace(tmp_path)
    path = tmp_path / "02_source_memory/indexes/typed_links.yml"
    registry = read_yaml(path)
    registry["current_pair_decisions"] = [{"source_ids": ["source-a", "source-b"], "status": "accepted", "active": True,
        "refresh_pending": pending, "relation_ids": ["synthetic-link"], "input_profile_hashes": {
            "source-a": "changed-hash" if changed else profile_content_hash(profiles[0]), "source-b": profile_content_hash(profiles[1])}}]
    write_yaml(path, registry)
    loaded = load_local_retrieval(tmp_path)
    if pending or changed:
        assert loaded["relationships"] == []
    else:
        assert loaded["relationships"][0]["freshness"] == "current_profile_hashes"


def test_legacy_relationship_freshness_is_explicit(tmp_path: Path) -> None:
    _workspace(tmp_path)
    loaded = load_local_retrieval(tmp_path)
    assert loaded["relationships"][0]["freshness"] == "recorded_unverified"
    assert loaded["status"]["recorded_unverified_relationships"] == 1


def test_large_cache_is_paged_without_raising_yaml_file_budget(tmp_path: Path) -> None:
    _workspace(tmp_path)
    scan_local_collection(tmp_path, instance_id="desktop-one", item_keys=["A"], client=LocalClient([_item("A")]))
    path = tmp_path / CACHE_RELATIVE_PATH
    with path.open("ab") as output:
        output.write(b"\x00" * (9 * 1024 * 1024))
    loaded = load_local_retrieval(tmp_path, instance_id="desktop-one")
    assert loaded["status"]["cached_document_count"] == 1
    assert loaded["status"]["cache_record_bytes"] < 100_000


def test_verified_catalogue_alias_binds_without_rewriting_cache_identity(tmp_path: Path) -> None:
    _workspace(tmp_path, count=1)
    path = tmp_path / "02_source_memory/indexes/source_catalogue.yml"
    catalogue = read_yaml(path)
    catalogue["sources"][0]["identity"] = {"canonical_zotero_key": "A", "zotero_item_keys": ["A", "ALIAS"]}
    write_yaml(path, catalogue)
    result = scan_local_collection(tmp_path, instance_id="desktop-one", item_keys=["ALIAS"], client=LocalClient([_item("ALIAS")]))
    assert result["documents"][0]["source_id"] == "source-a"
    assert result["documents"][0]["identity"]["item_key"] == "ALIAS"
    loaded = load_local_retrieval(tmp_path, instance_id="desktop-one")
    assert loaded["status"]["cached_document_count"] == 1
    assert loaded["index"].references["source-a"]


def test_loader_selects_current_profiles_and_ignores_orphan_duplicates(tmp_path: Path) -> None:
    _workspace(tmp_path)
    profiles = tmp_path / "02_source_memory/profiles"
    (profiles / "old-duplicate.yml").write_text("invalid historical duplicate: [")
    (profiles / "orphan.yml").write_bytes(b"x" * (8 * 1024 * 1024 + 1))
    loaded = load_local_retrieval(tmp_path, max_files=6)
    assert loaded["status"]["source_count"] == 2
    assert loaded["status"]["files_read"] == 6
    assert loaded["status"]["missing_profile_count"] == 0


def test_loader_projects_one_current_profile_at_a_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _workspace(tmp_path)
    from auto_zettelkasten import local_tools

    path = profile_sidecar_path(tmp_path / "02_source_memory/profiles", "note-a")
    wrapper = read_yaml(path)
    wrapper["profile"]["context"]["unused_rich_payload"] = "private synthetic payload " * 1000
    write_yaml(path, wrapper)
    original = local_tools.local_retrieval_projection
    calls = []

    def project(profiles, catalogue):
        assert len(profiles) == len(catalogue["sources"]) == 1
        assert profiles[0]["source_id"] == catalogue["sources"][0]["source_id"]
        calls.append(profiles[0]["source_id"])
        return original(profiles, catalogue)

    monkeypatch.setattr(local_tools, "local_retrieval_projection", project)
    loaded = load_local_retrieval(tmp_path)
    assert calls == ["source-a", "source-b"]
    assert "unused_rich_payload" not in str(loaded["rows"])


def test_missing_current_profile_is_partial_without_using_old_copies(tmp_path: Path) -> None:
    _workspace(tmp_path)
    path = profile_sidecar_path(tmp_path / "02_source_memory/profiles", "note-b")
    path.rename(path.with_name("old-copy.yml"))
    loaded = load_local_retrieval(tmp_path)
    assert loaded["status"]["catalogue_source_count"] == 2
    assert loaded["status"]["source_count"] == 1
    assert loaded["status"]["profile_load_status"] == "partial"
    assert loaded["status"]["missing_profile_source_ids"] == ["source-b"]


def test_alias_metadata_and_canonical_stub_do_not_create_false_ambiguity(tmp_path: Path) -> None:
    _workspace(tmp_path)
    path = tmp_path / "02_source_memory/indexes/source_catalogue.yml"
    catalogue = read_yaml(path)
    catalogue["sources"][1]["identity"] = {"canonical_zotero_key": "B", "zotero_item_keys": ["B", "BALIAS"], "doi": "10.1000/example"}
    write_yaml(path, catalogue)
    scan_local_collection(tmp_path, instance_id="desktop-one", item_keys=["BALIAS"], client=LocalClient([_item("BALIAS", DOI="10.1000/example")]))
    scan_local_collection(tmp_path, instance_id="desktop-one", item_keys=["A"], client=LocalClient([_item("A")]))
    cache = tmp_path / CACHE_RELATIVE_PATH
    before = cache.read_bytes()
    index = load_local_retrieval(tmp_path, instance_id="desktop-one")["index"]
    assert index.references["source-a"]["doi:10.1000/example"]["match_status"] == "matched_identifier"
    assert any(signal["signal"] == "direct_reference" for pair in index.candidate_pairs(max_pairs=1) for signal in pair["discovery_provenance"])
    assert cache.read_bytes() == before


def test_same_doi_on_different_sources_remains_ambiguous(tmp_path: Path) -> None:
    _workspace(tmp_path)
    scan_local_collection(tmp_path, instance_id="desktop-one", item_keys=["A", "B"],
                          client=LocalClient([_item("A", DOI="10.1000/example"), _item("B", DOI="10.1000/example")]))
    index = load_local_retrieval(tmp_path, instance_id="desktop-one")["index"]
    assert not index.direct


@pytest.fixture
def large_catalogue(tmp_path: Path) -> Path:
    _workspace(tmp_path)
    path = tmp_path / "02_source_memory/indexes/source_catalogue.yml"
    with path.open("a") as output:
        output.write("padding: " + "p" * (8 * 1024 * 1024 + 1024) + "\n")
    return tmp_path


def test_larger_compact_catalogue_is_admitted_for_recall_and_explicit_scan(large_catalogue: Path) -> None:
    loaded = load_local_retrieval(large_catalogue)
    assert loaded["status"]["source_count"] == 2
    assert loaded["status"]["bytes_read"] > 8 * 1024 * 1024
    scanned = scan_local_collection(large_catalogue, instance_id="desktop-one", item_keys=["A"], client=LocalClient([_item("A")]))
    assert scanned["documents"][0]["source_id"] == "source-a"


@pytest.mark.parametrize("bounds", [{"max_index_bytes": 8 * 1024 * 1024}, {"max_file_bytes": 8 * 1024 * 1024}, {"max_total_bytes": 8 * 1024 * 1024}])
def test_large_index_respects_explicit_limits_before_parsing(large_catalogue: Path, monkeypatch: pytest.MonkeyPatch, bounds: dict[str, int]) -> None:
    monkeypatch.setattr("auto_zettelkasten.local_tools.yaml.parse", lambda *_args, **_kwargs: pytest.fail("overbudget parse"))
    with pytest.raises(ValueError, match="limit"):
        load_local_retrieval(large_catalogue, **bounds)


def test_larger_index_budget_does_not_widen_rich_profile_limit(tmp_path: Path) -> None:
    _workspace(tmp_path, count=1)
    path = profile_sidecar_path(tmp_path / "02_source_memory/profiles", "note-a")
    path.write_bytes(b"x" * (8 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="per-file byte limit"):
        load_local_retrieval(tmp_path)


def test_index_event_limit_rejects_before_construction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _workspace(tmp_path)
    monkeypatch.setattr("auto_zettelkasten.local_tools.MAX_INDEX_YAML_NODES", 20)
    monkeypatch.setattr("auto_zettelkasten.local_tools.yaml.load", lambda *_args, **_kwargs: pytest.fail("overbudget construction"))
    with pytest.raises(ValueError, match="node limit"):
        load_local_retrieval(tmp_path)


@pytest.mark.parametrize("root", ["[]\n", "false\n", "0\n"])
def test_nonmapping_empty_index_roots_are_rejected(tmp_path: Path, root: str) -> None:
    path = tmp_path / "02_source_memory/indexes/source_catalogue.yml"
    path.parent.mkdir(parents=True)
    path.write_text(root)
    with pytest.raises(ValueError, match="mapping"):
        load_local_retrieval(tmp_path)
