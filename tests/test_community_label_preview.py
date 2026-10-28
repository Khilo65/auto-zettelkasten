"""Synthetic joint-label preview checks; no provider calls or private inputs."""
from copy import deepcopy
from decimal import Decimal
import hashlib
import socket

import pytest

from auto_zettelkasten.community_label_preview import (
    MAX_INPUT_TOKENS, MAX_OUTPUT_TOKENS, SYSTEM_PROMPT,
    prepare_joint_naming_preview, validate_joint_labels,
)


def source(sid, title, text):
    return {"source_id": sid, "title": title, "text": text,
            "content_sha256": hashlib.sha256(text.encode()).hexdigest()}


def inputs():
    corpus = [source("one", "School governance", "# Thesis\nSchool governance shapes access. Funding changes outcomes. A third sentence.\n# Links\nIgnored navigation."),
              source("two", "Public funding", "Public funding varies by region. Institutions affect provision."),
              source("three", "River ecosystems", "River ecosystems respond to pollution. Restoration changes habitat."),
              source("four", "Habitat restoration", "Habitat restoration depends on local conditions.")]
    communities = [{"community_id": "education", "source_ids": ["two", "one"]},
                   {"community_id": "ecology", "source_ids": ["four", "three"]}]
    proposals = {"version": "synthetic-v1", "labels": {
        "education": {"community_id": "education", "source_ids": ["one", "two"],
                      "candidates": [{"phrase": "School governance", "support_source_ids": ["one"]}],
                      "representative_source_ids": ["one", "two"]},
        "ecology": {"community_id": "ecology", "source_ids": ["four", "three"],
                    "candidates": [{"phrase": "River ecosystems", "support_source_ids": ["three"]}],
                    "representative_source_ids": ["three", "four"]}}}
    return communities, corpus, proposals


def prepare(**kwargs):
    return prepare_joint_naming_preview(*inputs(), provider="synthetic", model="pinned-small-v1", **kwargs)


def response():
    return {"labels": [{"community_id": "education", "title": "School governance and funding"},
                       {"community_id": "ecology", "title": "River restoration and ecosystems"}]}


def test_inert_unknown_price_is_not_free_and_inputs_are_preserved(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *args: pytest.fail("network attempted"))
    args = inputs()
    before = deepcopy(args)
    preview = prepare_joint_naming_preview(*args, provider="synthetic", model="pinned-small-v1")
    assert args == before
    assert preview["paid_dispatch_enabled"] is False
    assert preview["approval_required"] is True
    assert preview["reserved_cost_usd"] is None
    assert preview["pricing_status"] == "unknown"
    assert validate_joint_labels(response(), preview)["paid_dispatch_enabled"] is False


def test_prices_and_pinned_payload_are_explicit():
    preview = prepare(input_price_per_million="0.1", output_price_per_million="0.5",
                      pricing_identity="synthetic-rates-v1")
    assert Decimal(preview["reserved_cost_usd"]) == (
        Decimal(preview["reserved_input_tokens"]) * Decimal("0.1") + 1024 * Decimal("0.5")) / 1_000_000
    assert preview["reserved_output_tokens"] == MAX_OUTPUT_TOKENS
    assert preview["request_payload"]["model"] == "pinned-small-v1"
    assert preview["request_payload"]["max_tokens"] == 1024
    assert preview["request_payload"]["store"] is False
    assert "Mixed:" in SYSTEM_PROMPT and "Use null" not in SYSTEM_PROMPT
    assert "or null" in SYSTEM_PROMPT


@pytest.mark.parametrize("kwargs", [
    {"input_price_per_million": "0.1"},
    {"input_price_per_million": "0.1", "output_price_per_million": "0.2"},
    {"input_price_per_million": "NaN", "output_price_per_million": "0.2", "pricing_identity": "test"},
    {"input_price_per_million": -1, "output_price_per_million": "0.2", "pricing_identity": "test"},
    {"input_price_per_million": True, "output_price_per_million": "0.2", "pricing_identity": "test"},
])
def test_invalid_or_unidentified_prices_are_rejected(kwargs):
    with pytest.raises(ValueError):
        prepare(**kwargs)


def test_request_is_stable_but_model_partition_and_prices_change_identity():
    groups, corpus, proposals = inputs()
    first = prepare(partition_identity="broad")
    reordered = prepare_joint_naming_preview(groups[::-1], corpus[::-1], proposals,
        provider="synthetic", model="pinned-small-v1", partition_identity="broad")
    assert first["request_digest"] == reordered["request_digest"]
    assert first["request_digest"] != prepare(partition_identity="default")["request_digest"]
    assert first["request_digest"] != prepare_joint_naming_preview(groups, corpus, proposals,
        provider="synthetic", model="other-pin", partition_identity="broad")["request_digest"]
    priced = prepare(partition_identity="broad", input_price_per_million="0.1",
                     output_price_per_million="0.2", pricing_identity="test")
    assert first["request_digest"] != priced["request_digest"]


def test_all_members_bind_evidence_even_when_not_represented():
    groups, corpus, proposals = inputs()
    proposals["labels"]["education"]["representative_source_ids"] = ["one"]
    original = prepare_joint_naming_preview(groups, corpus, proposals, provider="s", model="m")
    corpus[1] = source("two", "Public funding", "Public funding has changed.")
    changed = prepare_joint_naming_preview(groups, corpus, proposals, provider="s", model="m")
    assert original["packet"] == changed["packet"]
    assert original["request_digest"] != changed["request_digest"]
    assert original["evidence_identity"] != changed["evidence_identity"]
    corpus.append(source("outside", "Outside topic", "Not in the selected sibling partition."))
    outside = prepare_joint_naming_preview(groups, corpus, proposals, provider="s", model="m")
    assert changed["request_digest"] == outside["request_digest"]


def test_packet_uses_compact_refs_and_two_sentence_excerpts():
    preview = prepare()
    reps = [r for group in preview["packet"]["communities"] for r in group["representatives"]]
    assert all(r["source_ref"].startswith("S000") for r in reps)
    assert all(len(r["thesis_excerpt"]) <= 500 for r in reps)
    assert not any("third sentence" in r["thesis_excerpt"] for r in reps)
    assert not any("Ignored navigation" in r["thesis_excerpt"] for r in reps)
    assert {r["source_ref"] for r in reps} == set(preview["identity"]["evidence"]["sources"])


def test_context_guard_shrinks_representatives_without_omitting_siblings():
    groups, corpus, proposals = [], [], {}
    for number in range(20):
        cid = f"group-{number:02}"
        ids = [f"source-{number}-{i}" for i in range(4)]
        groups.append({"community_id": cid, "source_ids": ids})
        for sid in ids:
            corpus.append(source(sid, "A" * 180, "A substantive topic " + "text " * 150))
        proposals[cid] = {"representative_source_ids": ids, "candidates": []}
    preview = prepare_joint_naming_preview(groups, corpus, proposals, provider="s", model="m")
    assert len(preview["packet"]["communities"]) == 20
    assert 20 <= preview["representative_count"] < 80
    assert preview["reserved_input_tokens"] <= MAX_INPUT_TOKENS
    assert all(1 <= len(group["representatives"]) <= 4 for group in preview["packet"]["communities"])


@pytest.mark.parametrize("mutation, match", [
    (lambda g, c, p: c[0].update(content_sha256="stale"), "hash"),
    (lambda g, c, p: c[0].update(text=123), "note text"),
    (lambda g, c, p: c[0].update(thesis="Invented thesis"), "excerpt"),
    (lambda g, c, p: c.append(deepcopy(c[0])), "duplicate"),
    (lambda g, c, p: g[0]["source_ids"].append("missing"), "bind"),
    (lambda g, c, p: g[0]["source_ids"].append("one"), "duplicate"),
    (lambda g, c, p: g.append({"community_id": "copy", "source_ids": ["one", "two"]}), "duplicate sibling"),
    (lambda g, c, p: g[1]["source_ids"].append("one"), "non-overlapping"),
    (lambda g, c, p: p["labels"]["education"].update(source_ids=["one"]), "stale"),
    (lambda g, c, p: p["labels"]["education"].update(source_ids=[{}]), "identity"),
    (lambda g, c, p: p["labels"]["education"].update(representative_source_ids=[{}]), "representatives"),
    (lambda g, c, p: p["labels"]["education"]["candidates"][0].update(support_source_ids=[{}]), "support"),
    (lambda g, c, p: p["labels"]["education"]["candidates"][0].update(support_source_ids=["three"]), "support"),
    (lambda g, c, p: p["labels"]["education"]["candidates"][0].update(phrase="Invented hypothesis"), "grounded"),
    (lambda g, c, p: p["labels"]["education"]["candidates"][0].update(phrase="---"), "grounded"),
])
def test_invalid_or_stale_evidence_fails_closed(mutation, match):
    args = inputs()
    mutation(*args)
    with pytest.raises(ValueError, match=match):
        prepare_joint_naming_preview(*args, provider="s", model="m")


def test_too_many_siblings_are_rejected_not_silently_truncated():
    groups = [{"community_id": str(i), "source_ids": [str(i)]} for i in range(25)]
    corpus = [source(str(i), "Synthetic topic", "Substantive evidence.") for i in range(25)]
    with pytest.raises(ValueError, match="sibling"):
        prepare_joint_naming_preview(groups, corpus, {}, provider="s", model="m")


def test_even_one_representative_per_sibling_must_fit_the_bound():
    groups, corpus, proposals = [], [], {}
    for number in range(24):
        cid = chr(0x1F300 + number) * 256
        sid = f"s{number}"
        groups.append({"community_id": cid, "source_ids": [sid]})
        corpus.append(source(sid, "書" * 180, "書" * 500))
        proposals[cid] = {"candidates": []}
    with pytest.raises(ValueError, match="32K"):
        prepare_joint_naming_preview(groups, corpus, proposals, provider="s", model="m")


def test_four_representative_limit_and_candidate_normalization():
    corpus = [source(str(i), "School governance", "School governance affects access.") for i in range(8)]
    groups = [{"community_id": "education", "source_ids": [str(i) for i in range(8)]}]
    proposals = {"education": {"representative_source_ids": [str(i) for i in range(8)],
                              "candidates": ["School governance", "SCHOOL GOVERNANCE"]}}
    preview = prepare_joint_naming_preview(groups, corpus, proposals, provider="s", model="m")
    assert preview["source_count"] == 8
    assert preview["representative_count"] == 4
    assert preview["packet"]["communities"][0]["candidate_phrases"] == ["School governance"]


def test_valid_labels_and_unresolved_groups_do_not_claim_semantic_verification():
    data = response()
    data["labels"][0]["title"] = "Mixed: funding and governance"
    data["labels"][1]["title"] = None
    result = validate_joint_labels(data, prepare())
    assert result["labels"]["education"] == "Mixed: funding and governance"
    assert result["unresolved"] == ["ecology"]
    assert result["semantic_correctness_verified"] is False


@pytest.mark.parametrize("title", ["Other", "Group 2", "Community9", "school, funding, access", "All sources agree on access", "Unanimous conservation findings", "---", 42])
def test_generic_keyword_or_consensus_titles_are_rejected(title):
    data = response()
    data["labels"][0]["title"] = title
    with pytest.raises(ValueError):
        validate_joint_labels(data, prepare())


def test_normalized_sibling_duplicates_are_rejected():
    data = response()
    data["labels"][0]["title"] = "School Governance"
    data["labels"][1]["title"] = "Ｓｃｈｏｏｌ—GOVERNANCE"
    with pytest.raises(ValueError, match="normalized"):
        validate_joint_labels(data, prepare())


@pytest.mark.parametrize("mutation", [
    lambda d: d["labels"].pop(),
    lambda d: d["labels"].append(deepcopy(d["labels"][0])),
    lambda d: d["labels"][0].update(community_id="unrequested"),
    lambda d: d["labels"][0].update(source_ids=["one"]),
    lambda d: d.update(explanation="extra output"),
])
def test_response_binds_exact_requested_groups_only(mutation):
    data = response()
    mutation(data)
    with pytest.raises(ValueError):
        validate_joint_labels(data, prepare())


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(paid_dispatch_enabled=True),
    lambda p: p["identity"].update(model="new-model"),
    lambda p: p["packet"]["communities"][0].update(member_count=99),
    lambda p: p["request_payload"].update(model="new-model"),
    lambda p: p.update(evidence_identity="stale"),
])
def test_tampered_preview_is_rejected(mutation):
    preview = prepare()
    mutation(preview)
    with pytest.raises(ValueError):
        validate_joint_labels(response(), preview)
