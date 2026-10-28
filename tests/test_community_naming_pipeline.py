"""Synthetic two-stage naming and cache checks; no paid calls or real notes."""
from copy import deepcopy
import hashlib
import json
import os
import socket
import sqlite3

import pytest

from auto_zettelkasten.community_naming import _digest
from auto_zettelkasten.community_naming_pipeline import (
    CommunityNamingCache, VERSION, finish_reconciliation,
    prepare_two_stage_naming, validate_final_result, validate_local_result,
)


def source(sid, title, text):
    return {"source_id": sid, "title": title, "text": text,
            "content_sha256": hashlib.sha256(text.encode()).hexdigest()}


def inputs():
    corpus = [source("school", "School access", "# School access\n\nSchools provide education. Funding affects access. More detail."),
              source("funding", "Public funding", "Public funding supports schooling."),
              source("river", "River habitats", "River habitats respond to restoration.")]
    communities = [{"community_id": "education", "source_ids": ["school", "funding"]},
                   {"community_id": "ecology", "source_ids": ["river"]}]
    return communities, corpus


def prepare(**kwargs):
    return prepare_two_stage_naming(*inputs(), **kwargs)


def drafts():
    return {"education": {"community_id": "education", "title": "Education funding", "scope": "School access and public funding."},
            "ecology": {"community_id": "ecology", "title": "River restoration", "scope": "Habitats and restoration."}}


def final():
    return {"labels": [{"community_id": "education", "title": "Education and public funding"},
                       {"community_id": "ecology", "title": "River habitat restoration"}]}


def provenance(preview):
    return {"origin": "imported_saved", "provider": preview["provider"], "model": preview["model"],
            "source_template_version": "synthetic-historical-v2", "evidence_identity": preview["evidence_identity"],
            "local_requests": {cid: {"request_sha256": "1" * 64, "response_sha256": "2" * 64} for cid in preview["local_jobs"]},
            "reconciliation_request_sha256": "3" * 64, "reconciliation_response_sha256": "4" * 64,
            "source_artifact_sha256": "5" * 64}


def generated(preview, job):
    return {"origin": "generated", "provider": preview["provider"], "model": preview["model"],
            "template_version": VERSION, "request_digest": job["request_digest"],
            "request_payload_sha256": _digest(job["request_payload"]), "response_sha256": "a" * 64}


def test_prepare_inert_complete_deterministic_and_not_mutating(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *args: pytest.fail("network attempted"))
    groups, corpus = inputs(); original = deepcopy((groups, corpus))
    preview = prepare_two_stage_naming(groups, corpus)
    assert (groups, corpus) == original
    assert preview["source_count"] == 3 and preview["paid_dispatch_enabled"] is False
    reordered = prepare_two_stage_naming(groups[::-1], corpus[::-1])
    assert reordered == preview
    for cid, job in preview["local_jobs"].items():
        assert job["request_payload"]["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}
        packet = json.loads(job["request_payload"]["messages"][1]["content"])
        group = next(g for g in groups if g["community_id"] == cid)
        assert {r["source_id"] for r in packet["members"]} == set(group["source_ids"])
        for row in packet["members"]:
            text = next(s["text"] for s in corpus if s["source_id"] == row["source_id"])
            assert row["thesis_excerpt"] in text


def test_no_sample_limit_of_twelve_members_or_twenty_four_groups():
    corpus = [source(f"s{i}", f"Topic {i}", f"Substantive topic {i}.") for i in range(26)]
    groups = [{"community_id": f"g{i}", "source_ids": [f"s{i}"]} for i in range(26)]
    assert prepare_two_stage_naming(groups, corpus)["community_count"] == 26
    one = prepare_two_stage_naming([{"community_id": "all", "source_ids": [s["source_id"] for s in corpus]}], corpus)
    packet = json.loads(one["local_jobs"]["all"]["request_payload"]["messages"][1]["content"])
    assert len(packet["members"]) == 26


def test_explicit_thesis_and_cautions_are_literal():
    groups, corpus = inputs()
    text = "> [!warning] Attribution is unresolved.\n\nExisting thesis states the main idea.\n\nOther content."
    corpus[0] = source("school", "School access", text)
    corpus[0].update(thesis="Existing thesis states the main idea.", source_cautions=["Attribution is unresolved."])
    p = prepare_two_stage_naming(groups, corpus)
    packet = json.loads(p["local_jobs"]["education"]["request_payload"]["messages"][1]["content"])
    row = next(row for row in packet["members"] if row["source_id"] == "school")
    assert row["thesis_excerpt"] == corpus[0]["thesis"]
    assert "Attribution is unresolved." in row["source_cautions"]
    corpus[0]["source_cautions"] = ["Invented warning"]
    with pytest.raises(ValueError, match="literal"):
        prepare_two_stage_naming(groups, corpus)


def test_context_failure_never_discards_members(monkeypatch):
    monkeypatch.setattr("auto_zettelkasten.community_naming_pipeline.MAX_INPUT_TOKENS", 10)
    with pytest.raises(ValueError, match="no community member was dropped"):
        prepare()


def test_reconciliation_only_contains_drafts_and_counts():
    p = prepare(); job = finish_reconciliation(p, drafts())
    packet = json.loads(job["request_payload"]["messages"][1]["content"])
    assert {r["community_id"] for r in packet["communities"]} == set(drafts())
    assert all(set(row) == {"community_id", "title", "scope", "member_count"} for row in packet["communities"])
    assert job["paid_dispatch_enabled"] is False
    duplicate_drafts = drafts(); duplicate_drafts["education"]["title"] = "River restoration"
    assert finish_reconciliation(p, duplicate_drafts)  # Reconciliation resolves draft collisions.


def test_import_reuses_without_impersonating_production_request(tmp_path):
    p = prepare(); cache = CommunityNamingCache(tmp_path / "names.sqlite")
    result = cache.import_stage_results(p, drafts(), final(), provenance(p))
    assert result["status"] == "cached" and result["provider_calls"] == 0
    assert result["imported_reuse_count"] == 3 and result["exact_request_cache_hits"] == 0
    assert result["final_provenance"]["source_template_version"] == "synthetic-historical-v2"
    assert cache.replay(p) == result
    with sqlite3.connect(cache.path) as db:
        assert db.execute("SELECT COUNT(*) FROM naming_exact_requests").fetchone()[0] == 0


def test_changed_one_note_preserves_other_draft_invalidates_reconciliation(tmp_path):
    groups, corpus = inputs(); p = prepare_two_stage_naming(groups, corpus)
    cache = CommunityNamingCache(tmp_path / "names.sqlite")
    cache.import_stage_results(p, drafts(), final(), provenance(p))
    corpus[0] = source("school", "School access", corpus[0]["text"] + " Changed evidence.")
    changed = prepare_two_stage_naming(groups, corpus)
    assert changed["local_jobs"]["ecology"] == p["local_jobs"]["ecology"]
    assert changed["local_jobs"]["education"] != p["local_jobs"]["education"]
    replay = cache.replay(changed)
    assert replay["status"] == "partial" and set(replay["drafts"]) == {"ecology"}
    assert replay["final"] is None and replay["missing_community_ids"] == ["education"]


def test_adding_sibling_preserves_existing_drafts(tmp_path):
    groups, corpus = inputs(); p = prepare_two_stage_naming(groups, corpus)
    cache = CommunityNamingCache(tmp_path / "names.sqlite")
    cache.import_stage_results(p, drafts(), final(), provenance(p))
    groups.append({"community_id": "health", "source_ids": ["health-source"]})
    corpus.append(source("health-source", "Public health", "Public health policy."))
    changed = prepare_two_stage_naming(groups, corpus)
    replay = cache.replay(changed)
    assert set(replay["drafts"]) == {"education", "ecology"}
    assert replay["missing_community_ids"] == ["health"] and replay["final"] is None


@pytest.mark.parametrize("field,value", [("provider", "other-provider"), ("model", "another-pin"), ("partition_identity", "broad")])
def test_model_and_partition_changes_invalidate_correct_level(tmp_path, field, value):
    p = prepare(); cache = CommunityNamingCache(tmp_path / "names.sqlite")
    cache.import_stage_results(p, drafts(), final(), provenance(p))
    changed = prepare(**{field: value}); replay = cache.replay(changed)
    if field == "partition_identity":
        assert len(replay["drafts"]) == 2 and replay["final"] is None
    else:
        assert not replay["drafts"] and replay["final"] is None


def test_generated_stages_have_exact_cache_origin(tmp_path):
    p = prepare(); cache = CommunityNamingCache(tmp_path / "names.sqlite")
    for cid, row in drafts().items():
        cache.put_local(p, cid, row, generated(p, p["local_jobs"][cid]))
    before = cache.replay(p)
    assert before["status"] == "partial" and before["exact_request_cache_hits"] == 2
    job = finish_reconciliation(p, drafts())
    cache.put_final(p, drafts(), final(), generated(p, job))
    after = cache.replay(p)
    assert after["status"] == "cached" and after["exact_request_cache_hits"] == 3
    assert after["imported_reuse_count"] == 0


def test_generated_receipt_cannot_bind_another_request(tmp_path):
    p = prepare(); cache = CommunityNamingCache(tmp_path / "names.sqlite")
    receipt = generated(p, p["local_jobs"]["ecology"])
    with pytest.raises(ValueError, match="receipt"):
        cache.put_local(p, "education", drafts()["education"], receipt)


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(model="changed"),
    lambda p: p["local_jobs"]["education"]["request_payload"]["messages"][0].update(content="changed"),
    lambda p: p.update(reconciliation_template="changed"),
    lambda p: p["local_jobs"]["education"].update(output_token_limit=999999),
    lambda p: p["local_jobs"]["education"].update(paid_dispatch_enabled=True),
    lambda p: p["local_jobs"]["education"].update(stage="reconciliation"),
])
def test_tampered_preview_rejected(mutation):
    p = prepare(); mutation(p)
    with pytest.raises(ValueError):
        finish_reconciliation(p, drafts())


@pytest.mark.parametrize("mutation", [
    lambda v: v["labels"].pop(),
    lambda v: v["labels"].append({"community_id": "unrequested", "title": "Another topic"}),
    lambda v: v["labels"][1].update(title="Ｅｄｕｃａｔｉｏｎ and public funding"),
    lambda v: v["labels"][0].update(title="bad\nlabel"),
    lambda v: v["labels"][0].update(title="Community 42"),
    lambda v: v["labels"][0].update(title="All sources agree on justice"),
])
def test_invalid_final_results_rejected(mutation):
    value = final(); mutation(value)
    with pytest.raises(ValueError):
        validate_final_result(value, prepare())


def test_null_and_scope_contract():
    assert validate_local_result({"community_id": "a", "title": None, "scope": None}, "a")["title"] is None
    with pytest.raises(ValueError, match="null"):
        validate_local_result({"community_id": "a", "title": None, "scope": "Scope"}, "a")


@pytest.mark.parametrize("field,value", [("model", "different"), ("origin", "generated"),
    ("source_template_version", VERSION), ("evidence_identity", "0" * 64), ("reconciliation_response_sha256", "bad")])
def test_import_provenance_cannot_relabel_origin_or_evidence(tmp_path, field, value):
    p = prepare(); receipt = provenance(p); receipt[field] = value
    with pytest.raises(ValueError):
        CommunityNamingCache(tmp_path / "cache.sqlite").import_stage_results(p, drafts(), final(), receipt)


def test_cache_checksum_and_immutable_results(tmp_path):
    p = prepare(); cache = CommunityNamingCache(tmp_path / "names.sqlite")
    cache.import_stage_results(p, drafts(), final(), provenance(p))
    altered = final(); altered["labels"][0]["title"] = "Another title"
    with pytest.raises(ValueError, match="immutable"):
        cache.import_stage_results(p, drafts(), altered, provenance(p))
    assert cache.replay(p)["status"] == "cached"
    with sqlite3.connect(cache.path) as db:
        db.execute("UPDATE naming_reuse SET result='{}' WHERE stage='reconciliation'")
    with pytest.raises(ValueError, match="checksum"):
        cache.replay(p)


def test_symlink_and_hardlink_cache_rejected(tmp_path):
    real = tmp_path / "real.sqlite"; CommunityNamingCache(real)
    linked = tmp_path / "linked.sqlite"; linked.symlink_to(real)
    with pytest.raises(ValueError, match="symbolic"):
        CommunityNamingCache(linked)
    hard = tmp_path / "hard.sqlite"; os.link(real, hard)
    with pytest.raises(ValueError, match="dedicated"):
        CommunityNamingCache(hard)


def test_stale_source_hash_rejected():
    groups, corpus = inputs(); corpus[0]["text"] += " Changed."
    with pytest.raises(ValueError, match="hash"):
        prepare_two_stage_naming(groups, corpus)
