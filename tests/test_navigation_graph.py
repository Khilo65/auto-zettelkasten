from __future__ import annotations

from copy import deepcopy

import pytest

from auto_zettelkasten.navigation import (
    TYPED_SOURCE_RELATIONS,
    build_navigation_graph,
    build_typed_source_relations,
    canonicalize_subject_tag,
    derive_subject_tags,
    promote_topic_neighborhoods,
    rank_human_related_links,
    rank_topic_neighborhoods,
)
from auto_zettelkasten.models import NeighborhoodSummary


def _profile(source_id: str, **values: object) -> dict[str, object]:
    return {
        "source_id": source_id,
        "note_id": f"note-{source_id}",
        "title": f"Source {source_id}",
        "study_family_id": f"family-{source_id}",
        "note_status": "analytical_atomic_note",
        **values,
    }


def test_safe_subject_canonicalization_reconciles_mechanical_variants_only() -> None:
    plural = canonicalize_subject_tag("concept", " Civil Wars ")
    singular = canonicalize_subject_tag("concept", "civil-war")
    assert plural is not None
    assert plural["subject_tag_id"] == singular["subject_tag_id"]
    assert plural["canonical_tag"] == "concept/civil-war"

    impartiality = canonicalize_subject_tag("concept", "Mediator impartiality")
    neutrality = canonicalize_subject_tag("concept", "Mediator neutrality")
    assert impartiality is not None and neutrality is not None
    assert impartiality["subject_tag_id"] != neutrality["subject_tag_id"]

    assert canonicalize_subject_tag("concept", "mediation") is None
    assert canonicalize_subject_tag("concept", "electronic-books") is None
    assert canonicalize_subject_tag("concept", "Does mediator legitimacy improve every outcome?") is None
    assert canonicalize_subject_tag("concept", "https://example.com/tag") is None
    with pytest.raises(ValueError, match="unsupported subject-tag facet"):
        canonicalize_subject_tag("unsupported", "value")


def test_subject_tags_use_all_profile_dimensions_and_confirm_zotero_tags() -> None:
    profiles = [
        _profile(
            "A",
            concepts=["Ceasefire design", "mediation"],
            theories=["Ripeness theory"],
            mechanisms=["Security arrangements"],
            outcomes=["Agreement durability"],
            cases=["Syria"],
            populations=["Civilian participants"],
            geography=["Middle East"],
            periods=["Post-Cold War"],
            methods=["Process tracing"],
            data=["Peace agreement texts"],
            measures=["Implementation score"],
            normalized_tags=["ceasefire-design", "electronic-books", "orphan-zotero-tag"],
        )
    ]

    result = derive_subject_tags(profiles, max_visible_per_source=20)
    canonical = {row["canonical_tag"] for row in result["subject_tags"]}
    assert {
        "concept/ceasefire-design",
        "theory/ripeness-theory",
        "mechanism/security-arrangement",
        "outcome/agreement-durability",
        "case/syria",
        "population/civilian-participant",
        "geography/middle-east",
        "period/post-cold-war",
        "method/process-tracing",
        "data/peace-agreement-texts",
        "measure/implementation-score",
    }.issubset(canonical)
    assert all(not value.endswith("/mediation") for value in canonical)
    ceasefire = next(row for row in result["assignments"] if row["canonical_tag"] == "concept/ceasefire-design")
    assert "profile.concepts" in ceasefire["provenance"]
    assert "zotero.normalized_tags" in ceasefire["provenance"]
    assert not any(row["canonical_tag"].endswith("orphan-zotero-tag") for row in result["assignments"])
    assert result["unconfirmed_zotero_tag_count"] == 1
    assert any(row["reason"] == "bibliographic_format" for row in result["rejected_candidates"])


def test_normalized_dimensions_and_features_are_supported_without_source_rereads() -> None:
    result = derive_subject_tags(
        [
            _profile(
                "A",
                dimensions={
                    "mechanism": ["Mediator legitimacy"],
                    "method": ["Logit models"],
                    "case": ["Syria"],
                    "outcome": ["Mediation success"],
                },
                features={
                    "concepts": ["Third-party leverage"],
                    "zotero_tag_context": ["third-party-leverage"],
                },
            )
        ]
    )
    tags = {row["canonical_tag"] for row in result["subject_tags"]}
    assert "mechanism/mediator-legitimacy" in tags
    assert "method/logit-models" in tags
    assert "case/syria" in tags
    assert "outcome/mediation-success" in tags
    assert "concept/third-party-leverage" in tags


def test_candidate_and_visible_caps_are_deterministic_and_ids_ignore_order() -> None:
    profile = _profile("A", concepts=[f"Specific concept {index}" for index in range(40)])
    salient = [f"concept/specific-concept-{index}" for index in range(40)]
    first = derive_subject_tags(
        [profile],
        max_candidates_per_source=24,
        max_visible_per_source=8,
        cluster_salient_tags=salient,
    )
    reordered = deepcopy(profile)
    reordered["concepts"] = list(reversed(reordered["concepts"]))
    second = derive_subject_tags(
        [reordered],
        max_candidates_per_source=24,
        max_visible_per_source=8,
        cluster_salient_tags=salient,
    )

    assert first == second
    assert len(first["candidates"]) == 24
    assert sum(row["visible"] for row in first["assignments"]) == 8
    assert all(row["assignment_id"].startswith("subject-tag-assignment-") for row in first["assignments"])
    assert all(len(row["revision_hash"]) == 64 for row in first["subject_tags"])


def test_case_only_tag_variants_have_a_stable_total_order() -> None:
    result = derive_subject_tags(
        [
            _profile(
                "A",
                concepts=["Early warning"],
                normalized_tags=["early warning"],
            )
        ]
    )

    assignment = next(
        row for row in result["assignments"] if row["canonical_tag"] == "concept/early-warning"
    )
    registry = next(
        row for row in result["subject_tags"] if row["canonical_tag"] == "concept/early-warning"
    )
    assert assignment["original_variants"] == ["Early warning", "early warning"]
    assert registry["original_variants"] == ["Early warning", "early warning"]


def test_citations_zotero_relations_and_similarity_have_distinct_types() -> None:
    profiles = [
        _profile(
            "A",
            zotero_item_key="AKEY",
            concepts=["Mediator legitimacy"],
            outcomes=["Mediation success"],
            citation_relations={"cites": ["BKEY"]},
            zotero_relations={"dc:relation": ["CKEY"]},
        ),
        _profile(
            "B",
            zotero_item_key="BKEY",
            concepts=["Mediator legitimacy"],
            outcomes=["Mediation success"],
        ),
        _profile("C", zotero_item_key="CKEY", concepts=["Unrelated concept"]),
    ]
    tags = derive_subject_tags(profiles)
    relations = build_typed_source_relations(profiles, tag_assignments=tags["assignments"])
    typed = {(row["source_id"], row["target_source_id"], row["relation_type"]) for row in relations}

    assert ("A", "B", "cites") in typed
    assert ("B", "A", "cited_by") in typed
    assert ("A", "C", "zotero_related") in typed
    assert any(row["relation_type"] == "shared_concept" for row in relations)
    assert any(row["relation_type"] == "same_outcome" for row in relations)
    assert any(row["relation_type"] == "semantic_similarity" for row in relations)
    assert {row["relation_type"] for row in relations}.issubset(TYPED_SOURCE_RELATIONS)


def test_related_note_reasons_deduplicate_labels_and_do_not_call_cases_concepts() -> None:
    profiles = [
        _profile(
            source_id,
            concepts=["Internationalized civil war"],
            cases=["Internationalized civil war"],
        )
        for source_id in ("A", "B")
    ]
    tags = derive_subject_tags(profiles)
    relations = build_typed_source_relations(profiles, tag_assignments=tags["assignments"])

    shared_concept = next(row for row in relations if row["relation_type"] == "shared_concept")
    assert shared_concept["evidence"][0]["subject_tags"] == [
        "concept/internationalized-civil-war"
    ]
    links = rank_human_related_links("A", profiles, relations)
    assert links[0]["reason"] == "Shared concept: internationalized civil war."


def test_broad_or_single_shared_tags_do_not_create_all_pairs_links() -> None:
    profiles = [_profile(source_id) for source_id in ("A", "B", "C")]
    assignments = [
        {
            "source_id": source_id,
            "subject_tag_id": "broad",
            "canonical_tag": "concept/mediation",
            "facet_type": "concept",
            "promotion_status": "promoted",
        }
        for source_id in ("A", "B", "C")
    ]
    assignments.extend(
        {
            "source_id": source_id,
            "subject_tag_id": "specific",
            "canonical_tag": "concept/mediator-legitimacy",
            "facet_type": "concept",
            "promotion_status": "promoted",
        }
        for source_id in ("A", "B", "C")
    )
    assert build_typed_source_relations(profiles, tag_assignments=assignments) == []


def test_same_case_method_outcome_and_proposition_relations_are_bounded() -> None:
    profiles = [
        _profile(
            chr(65 + index),
            cases=["Syria"],
            methods=["Process tracing"],
            outcomes=["Agreement durability"],
        )
        for index in range(6)
    ]
    tags = derive_subject_tags(profiles)
    propositions = [
        {
            "proposition_id": "proposition-1",
            "statement": "Monitoring affects agreement durability.",
            "source_ids": [profile["source_id"] for profile in profiles],
        }
    ]
    relations = build_typed_source_relations(
        profiles,
        tag_assignments=tags["assignments"],
        propositions=propositions,
        max_inferred_links_per_source=2,
    )
    assert {"same_case", "same_method", "same_outcome", "same_proposition"}.issubset(
        {row["relation_type"] for row in relations}
    )
    neighbors: dict[str, set[str]] = {str(profile["source_id"]): set() for profile in profiles}
    for row in relations:
        neighbors[row["source_id"]].add(row["target_source_id"])
        neighbors[row["target_source_id"]].add(row["source_id"])
    assert all(len(values) <= 2 for values in neighbors.values())


def test_neighborhoods_need_two_analytical_study_families_and_keep_limited_context() -> None:
    profiles = [
        _profile("A", concepts=["Ceasefire monitoring"]),
        _profile("B", concepts=["Ceasefire monitoring"]),
        _profile(
            "C",
            concepts=["Ceasefire monitoring"],
            note_status="abstract_only_atomic_note",
            study_family_id="family-C",
        ),
        _profile("D", concepts=["Mediator neutrality"], study_family_id="same-family"),
        _profile("E", concepts=["Mediator neutrality"], study_family_id="same-family"),
        _profile("F", concepts=["Singleton concept"]),
    ]
    tags = derive_subject_tags(profiles)
    result = promote_topic_neighborhoods(profiles, tags["subject_tags"], tags["assignments"])

    monitoring = next(
        row for row in result["topic_neighborhoods"] if row["semantic_identity"] == "ceasefire-monitoring"
    )
    assert monitoring["independent_source_count"] == 2
    assert monitoring["source_ids"] == ["A", "B", "C"]
    assert next(row for row in monitoring["member_relationship_reasons"] if row["source_id"] == "C")[
        "context_only"
    ]
    neutrality = next(row for row in result["singleton_facets"] if row["canonical_tag"].endswith("neutrality"))
    assert neutrality["reason"] == "duplicate_study_family_only"
    assert any(row["canonical_tag"].endswith("singleton-concept") for row in result["singleton_facets"])


def test_cluster_neighborhood_ranking_requires_two_cluster_members_and_is_capped() -> None:
    neighborhoods = [
        {
            "topic_neighborhood_id": f"n-{index}",
            "canonical_tag_id": f"tag-{index}",
            "semantic_identity": f"specific-topic-{index}",
            "source_ids": ["A", "B"] if index < 10 else ["A", "C"],
            "promotion_status": "promoted",
        }
        for index in range(11)
    ]
    ranked = rank_topic_neighborhoods(
        neighborhoods,
        ["A", "B"],
        proposition_tag_ids=["tag-8"],
        max_visible=8,
    )
    assert len(ranked) == 8
    assert ranked[0]["canonical_tag_id"] == "tag-8"
    assert all(row["cluster_member_count"] == 2 for row in ranked)


def test_human_related_links_include_reasons_and_do_not_cap_explicit_links() -> None:
    profiles = [_profile(source_id) for source_id in ("A", "B", "C", "D")]
    relations = [
        {
            "relation_id": "explicit-1",
            "source_id": "A",
            "target_source_id": "B",
            "relation_type": "cites",
            "evidence": [],
            "inferred": False,
            "strength": 100,
        },
        {
            "relation_id": "explicit-2",
            "source_id": "A",
            "target_source_id": "C",
            "relation_type": "zotero_related",
            "evidence": [],
            "inferred": False,
            "strength": 95,
        },
        {
            "relation_id": "inferred-1",
            "source_id": "A",
            "target_source_id": "D",
            "relation_type": "same_proposition",
            "evidence": [{"statement": "Monitoring affects agreement durability."}],
            "inferred": True,
            "strength": 90,
        },
    ]
    links = rank_human_related_links("A", profiles, relations, max_inferred_links=0)
    assert {row["target_source_id"] for row in links} == {"B", "C"}
    assert all(row["reason"] and row["target_title"] for row in links)
    assert all(row["explicit"] for row in links)


def test_navigation_graph_is_idempotent_and_keeps_projection_hash_separate() -> None:
    profiles = [
        _profile("A", concepts=["Mediator legitimacy"], cases=["Syria"]),
        _profile("B", concepts=["Mediator legitimacy"], cases=["Syria"]),
    ]
    first = build_navigation_graph(profiles)
    second = build_navigation_graph(list(reversed(profiles)))
    assert first == second
    assert len(first["graph_projection_hash"]) == 64
    assert first["promoted_neighborhood_count"] == 2
    assert "citation_or_relation" not in first["typed_relation_counts"]


def test_active_vocabulary_merges_safe_spelling_variants() -> None:
    profiles = [
        _profile("A", concepts=["Organisational behaviour"]),
        _profile("B", concepts=["Organizational behavior"]),
    ]
    result = derive_subject_tags(profiles)

    concepts = [row for row in result["subject_tags"] if row["facet_type"] == "concept"]
    assert len(concepts) == 1
    assert concepts[0]["canonical_tag"] == "concept/organizational-behavior"
    assert concepts[0]["graph_active"] is True
    assert concepts[0]["eligible_study_family_count"] == 2
    assert result["navigation_metrics"]["safe_alias_count"] >= 1
    assert result["tag_concept_registry"] == []
    assert result["tag_reconciliation_proposals"] == []


def test_singleton_facets_remain_in_audit_but_not_native_tags() -> None:
    result = derive_subject_tags([_profile("A", concepts=["Highly specific singleton"])])

    assignment = result["assignments"][0]
    registry = result["subject_tags"][0]
    assert assignment["promotion_status"] == "source_local_only"
    assert assignment["visible"] is False
    assert registry["graph_active"] is False
    assert result["active_subject_tags"] == []
    assert result["navigation_metrics"]["source_local_singleton_tag_count"] == 1


def test_semantic_synonyms_remain_separate_without_speculative_proposals() -> None:
    result = derive_subject_tags(
        [
            _profile("A", concepts=["Mediator impartiality"]),
            _profile("B", concepts=["Mediator neutrality"]),
        ]
    )

    concepts = {row["canonical_tag"]: row for row in result["subject_tags"]}
    assert concepts["concept/mediator-impartiality"]["subject_tag_id"] != concepts[
        "concept/mediator-neutrality"
    ]["subject_tag_id"]
    assert result["tag_reconciliation_proposals"] == []
    assert result["navigation_metrics"]["unresolved_reconciliation_count"] == 0


def test_incidental_same_case_alone_creates_neighborhood_not_direct_link() -> None:
    profiles = [_profile("A", cases=["Syria"]), _profile("B", cases=["Syria"])]
    graph = build_navigation_graph(profiles)

    assert graph["typed_relations"] == []
    assert len(graph["topic_neighborhoods"]) == 1
    assert graph["topic_neighborhoods"][0]["facet_type"] == "case"
    assert graph["human_neighborhood_summaries"][0]["why_useful"]
    assert NeighborhoodSummary.from_dict(graph["human_neighborhood_summaries"][0])


def test_same_proposition_creates_direct_link_without_shared_facets() -> None:
    profiles = [
        _profile("A", concepts=["Ceasefire design"]),
        _profile("B", concepts=["Third-party guarantees"]),
    ]
    relations = build_typed_source_relations(
        profiles,
        propositions=[
            {
                "proposition_id": "proposition-monitoring",
                "statement": "Monitoring affects agreement durability.",
                "source_ids": ["A", "B"],
            }
        ],
    )

    assert [row["relation_type"] for row in relations] == ["same_proposition"]
