from copy import deepcopy
import hashlib

import pytest

from auto_zettelkasten.community_labels import propose_community_labels
from auto_zettelkasten.community_label_embeddings import rerank_community_labels


SPACE = "synthetic-minilm-pinned-space-v1"


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def source(sid, title, body=""):
    return {"source_id": sid, "title": title, "text": body, "content_sha256": sha(body)}


def vector_row(row, vector):
    return {"source_id": row["source_id"], "content_sha256": row["content_sha256"],
            "embedding_identity": SPACE, "vector": vector}


def example():
    corpus = [source("a1", "Security sector reform pathways", "Security sector reform and military integration."),
              source("a2", "Security sector reform lessons", "Security sector reform and military integration."),
              source("b1", "Customary justice practices", "Customary justice and plural policing."),
              source("b2", "Customary justice institutions", "Customary justice and plural policing.")]
    groups = [{"community_id": "a", "source_ids": ["a1", "a2"], "display_label": "Original A"},
              {"community_id": "b", "source_ids": ["b1", "b2"], "display_label": "Original B"}]
    proposals = propose_community_labels(groups, corpus)
    notes = [vector_row(row, [1., 0.] if row["source_id"].startswith("a") else [0., 1.]) for row in corpus]
    phrases = []
    for phrase in sorted({c["phrase"] for row in proposals["labels"].values() for c in row["candidates"]}):
        phrases.append({"phrase": phrase, "content_sha256": sha(phrase), "embedding_identity": SPACE,
                        "vector": [1., 0.] if "security" in phrase.lower() else [0., 1.]})
    return groups, corpus, proposals, notes, phrases


def run(values, **kwargs):
    return rerank_community_labels(*values, embedding_identity=SPACE, **kwargs)


def test_semantic_ranking_exposes_grounded_fit_and_no_mutation():
    values = example()
    before = deepcopy(values)
    result = run(values)
    assert values == before
    assert result["labels"]["a"]["selected_candidate"]["semantic_fit"] == 1
    assert result["labels"]["b"]["selected_candidate"]["semantic_fit"] == 1
    assert "security" in result["labels"]["a"]["display_label"].lower()
    assert "customary" in result["labels"]["b"]["display_label"].lower()
    assert not result["diagnostics"]["membership_changed"]
    assert not result["diagnostics"]["links_changed"]
    assert result["diagnostics"]["model_calls"] == 0


def test_sibling_contrast_can_prefer_more_distinct_phrase():
    values = list(example())
    for row in values[3]:
        row["vector"] = [1., 0.] if row["source_id"].startswith("a") else [.8, .6]
    a_candidates = values[2]["labels"]["a"]["candidates"]
    first, second = a_candidates[:2]
    for row in values[4]:
        row["vector"] = ([1., 0.] if row["phrase"] == first["phrase"] else
                         [.8, -.6] if row["phrase"] == second["phrase"] else [-1., 0.])
    semantic = run(values, contrast_weight=0)
    contrast = run(values, contrast_weight=.5)
    assert semantic["labels"]["a"]["selected_candidate"]["phrase"] == first["phrase"]
    assert contrast["labels"]["a"]["selected_candidate"]["phrase"] == second["phrase"]
    assert semantic["identity"] != contrast["identity"]


def test_mmr_retains_diverse_alternatives():
    values = example()
    candidates = values[2]["labels"]["a"]["candidates"]
    assert len(candidates) >= 3
    for row in values[4]:
        row["vector"] = ([1., 0.] if row["phrase"] == candidates[0]["phrase"] else
                         [.99, .1] if row["phrase"] == candidates[1]["phrase"] else
                         [.4, .9] if row["phrase"] == candidates[2]["phrase"] else [-1., 0.])
    plain = run(values, diversity=0, top_n=2)
    diverse = run(values, diversity=.7, top_n=2)
    assert plain["labels"]["a"]["candidates"][1]["phrase"] == candidates[1]["phrase"]
    assert diverse["labels"]["a"]["candidates"][1]["phrase"] == candidates[2]["phrase"]


def test_same_space_positive_scaling_and_order_invariant():
    values = example()
    expected = run(values)
    groups, corpus, proposals, notes, phrases = deepcopy(values)
    for row in notes + phrases:
        row["vector"] = [v * 8 for v in row["vector"]]
    for group in groups:
        group["source_ids"].reverse()
    actual = run((list(reversed(groups)), list(reversed(corpus)), proposals,
                  list(reversed(notes)), list(reversed(phrases))))
    assert actual == expected


@pytest.mark.parametrize("kind", ["notes", "phrases"])
@pytest.mark.parametrize("change", ["missing", "duplicate", "hash", "identity", "dimension", "nan", "zero", "boolean", "unknown"])
def test_invalid_or_incompatible_vectors_rejected(kind, change):
    values = example()
    rows = values[3 if kind == "notes" else 4]
    if change == "missing":
        rows.pop()
    elif change == "duplicate":
        rows.append(deepcopy(rows[0]))
    elif change == "hash":
        rows[0]["content_sha256"] = "0" * 64
    elif change == "identity":
        rows[0]["embedding_identity"] = "different-model-or-preparation"
    elif change == "dimension":
        rows[0]["vector"] = [1., 0., 0.]
    elif change == "nan":
        rows[0]["vector"] = [float("nan"), 0.]
    elif change == "zero":
        rows[0]["vector"] = [0., 0.]
    elif change == "boolean":
        rows[0]["vector"] = [True, 0.]
    else:
        rows[0]["source_id" if kind == "notes" else "phrase"] = "unknown"
    with pytest.raises(ValueError):
        run(values)


def test_cross_kind_dimension_mismatch_rejected():
    values = example()
    for row in values[4]:
        row["vector"] = [1., 0., 0.]
    with pytest.raises(ValueError, match="dimensions"):
        run(values)


@pytest.mark.parametrize("change", ["phrase", "members", "corpus", "evidence"])
def test_stale_or_ungrounded_proposal_rejected(change):
    values = example()
    if change == "phrase":
        values[2]["labels"]["a"]["candidates"][0]["phrase"] = "Fabricated scientific topic"
    elif change == "members":
        values[0][0]["source_ids"].pop()
    elif change == "corpus":
        values[1][0]["text"] += " new evidence"
    else:
        values[2]["labels"]["a"]["candidates"][0]["evidence"][0]["start"] += 1
    with pytest.raises(ValueError):
        run(values)


def test_mixed_content_is_warning_not_claim_of_semantic_exclusivity():
    corpus = [source("a", "Customary justice"), source("b", "Solar magnetic fields"),
              source("c", "Mountain glacier dynamics")]
    groups = [{"community_id": "mixed", "source_ids": ["a", "b", "c"], "display_label": "Mixed original"}]
    proposals = propose_community_labels(groups, corpus)
    notes = [vector_row(row, [1., 0.]) for row in corpus]
    phrases = [{"phrase": c["phrase"], "content_sha256": sha(c["phrase"]), "embedding_identity": SPACE,
                "vector": [1., 0.]} for c in proposals["labels"]["mixed"]["candidates"]]
    result = run((groups, corpus, proposals, notes, phrases))
    record = result["labels"]["mixed"]
    assert record["status"] == "mixed_review_required"
    assert record["diagnostics"]["mixed_content_warning"]
    assert not record["diagnostics"]["semantic_exclusivity_certified"]


def test_opposing_member_vectors_fail_open_instead_of_inventing_centroid():
    values = example()
    values[3][1]["vector"] = [-1., 0.]
    with pytest.raises(ValueError, match="nonzero"):
        run(values)


@pytest.mark.parametrize("kwargs", [
    {"contrast_weight": -.1}, {"contrast_weight": float("inf")}, {"diversity": 1.1},
    {"diversity": True}, {"top_n": 0}, {"top_n": 9}, {"top_n": True},
])
def test_configuration_boundaries(kwargs):
    with pytest.raises(ValueError):
        run(example(), **kwargs)


def test_no_candidates_and_empty_partition_are_explicit():
    corpus = [source("a", "Research summary"), source("b", "Research summary")]
    groups = [{"community_id": "a", "source_ids": ["a"], "display_label": "Unknown A"},
              {"community_id": "b", "source_ids": ["b"], "display_label": "Unknown B"}]
    proposals = propose_community_labels(groups, corpus)
    assert all(not r["candidates"] for r in proposals["labels"].values())
    notes = [vector_row(row, [1., 0.]) for row in corpus]
    result = run((groups, corpus, proposals, notes, []))
    assert all(r["status"] == "unresolved" for r in result["labels"].values())
    empty = run(([], [], propose_community_labels([], []), [], []))
    assert empty["labels"] == {}
