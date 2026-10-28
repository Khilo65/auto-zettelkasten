from copy import deepcopy

import pytest

from auto_zettelkasten.community_labeling import (
    bind_naming_corpus, prepare_label_update, project_label_update,
)


def fixture():
    groups = [{"community_id": "a", "source_ids": ["s1"], "display_label": "old"}]
    mapping = {"sources": [{"source_id": "s1", "note_id": "n1", "note_path": "notes/a.md"}],
               "communities": groups, "community_scales": [{"scale": "fine", "communities": deepcopy(groups)}],
               "neighbors": [{"source": "s1", "target": "s2", "score": .8}],
               "identity": {"model": "frozen"}, "accepted_relationships": [{"kind": "supports"}]}
    snapshot = {"documents": [{"path": "notes/a.md", "revision": "revision1",
                                "frontmatter": {"note_id": "n1", "title": "Electoral violence"},
                                "text": "# Electoral violence\nElectoral violence involves contested elections."}]}
    corpus = bind_naming_corpus(mapping, snapshot)
    return mapping, snapshot, corpus


def test_projection_changes_only_labels_and_mirrors():
    mapping, _, corpus = fixture()
    before = deepcopy(mapping)
    prepared = prepare_label_update(mapping, corpus)
    assert prepared["aliases"]["fine"] == "default"
    result = project_label_update(mapping, corpus, prepared)
    assert mapping == before
    assert result["communities"] == result["community_scales"][0]["communities"]
    for key in ("sources", "neighbors", "identity", "accepted_relationships"):
        assert result[key] == mapping[key]
    assert result["communities"][0]["source_ids"] == ["s1"]


def test_review_bound_to_partition_evidence_and_identities():
    mapping, _, corpus = fixture()
    prepared = prepare_label_update(mapping, corpus)
    review = {"default": {"a": {"title": "Election conflict", "source_ids": ["s1"],
                                 "input_identity": prepared["partitions"]["default"]["input_identity"]}}}
    result = project_label_update(mapping, corpus, prepared, reviewed=review)
    assert result["communities"][0]["display_label"] == "Election conflict"
    review["default"]["a"]["source_ids"] = ["s2"]
    with pytest.raises(ValueError, match="mismatched"):
        project_label_update(mapping, corpus, prepared, reviewed=review)


@pytest.mark.parametrize("change", ["mapping", "corpus"])
def test_stale_evidence_rejected(change):
    mapping, _, corpus = fixture()
    prepared = prepare_label_update(mapping, corpus)
    if change == "mapping":
        mapping["neighbors"].append({"source": "s2"})
    else:
        corpus[0]["revision"] = "new-revision"
    with pytest.raises(ValueError, match="Stale"):
        project_label_update(mapping, corpus, prepared)


@pytest.mark.parametrize("case", ["wrong-id", "duplicate", "missing", "truncated"])
def test_binding_rejects_incomplete_or_ambiguous_snapshots(case):
    mapping, snapshot, _ = fixture()
    if case == "wrong-id":
        snapshot["documents"][0]["frontmatter"]["note_id"] = "n2"
    elif case == "duplicate":
        snapshot["documents"] *= 2
    elif case == "missing":
        snapshot["documents"] = []
    else:
        snapshot["truncated"] = True
    with pytest.raises(ValueError):
        bind_naming_corpus(mapping, snapshot)


def test_explicit_source_identity_conflict_is_rejected():
    mapping, snapshot, _ = fixture()
    snapshot["documents"][0]["frontmatter"]["source_id"] = "another-source"
    with pytest.raises(ValueError, match="source identity"):
        bind_naming_corpus(mapping, snapshot)


def test_modified_preparation_is_not_a_review():
    mapping, _, corpus = fixture()
    prepared = prepare_label_update(mapping, corpus)
    prepared["partitions"]["default"]["labels"]["a"]["display_label"] = "Invented topic"
    with pytest.raises(ValueError, match="does not match"):
        project_label_update(mapping, corpus, prepared)


def test_duplicate_normalized_review_titles_rejected():
    mapping, _, corpus = fixture()
    corpus.append({**corpus[0], "source_id": "s2", "revision": "other"})
    mapping["communities"].append({"community_id": "b", "source_ids": ["s2"], "display_label": "different"})
    mapping["community_scales"] = []
    prepared = prepare_label_update(mapping, corpus)
    identity = prepared["partitions"]["default"]["input_identity"]
    reviews = {"default": {"a": {"title": "Security reform", "source_ids": ["s1"], "input_identity": identity},
                           "b": {"title": "Ｓｅｃｕｒｉｔｙ reform", "source_ids": ["s2"], "input_identity": identity}}}
    with pytest.raises(ValueError, match="Duplicate"):
        project_label_update(mapping, corpus, prepared, reviewed=reviews)
