from __future__ import annotations

from collections import defaultdict
from itertools import combinations
import hashlib
import json
import re
import unicodedata
from typing import Any, Mapping, Sequence


TAG_RECONCILIATION_VERSION = "2"
NAVIGATION_RELATION_VERSION = "2"
NEIGHBORHOOD_PROMOTION_VERSION = "2"

SUBJECT_FACETS = (
    "concept",
    "theory",
    "mechanism",
    "outcome",
    "case",
    "population",
    "geography",
    "period",
    "method",
    "data",
    "measure",
)

TYPED_SOURCE_RELATIONS = {
    "cites",
    "cited_by",
    "zotero_related",
    "same_proposition",
    "shared_concept",
    "same_case",
    "same_method",
    "same_outcome",
    "semantic_similarity",
}

_FACET_FIELDS = {
    "concept": ("concept", "concepts", "key_concepts", "semantic_topics", "topics", "topic", "themes"),
    "theory": ("theory", "theories", "theoretical_framework", "theoretical_frameworks"),
    "mechanism": ("mechanism", "mechanisms"),
    "outcome": ("outcome", "outcomes", "dependent_variable", "dependent_variables"),
    "case": ("case", "cases", "setting", "settings", "country", "countries"),
    "population": ("population", "populations", "sample", "samples"),
    "geography": ("geography", "geographies", "region", "regions", "location", "locations"),
    "period": ("period", "periods", "time_period", "time_periods", "year", "years"),
    "method": ("method", "methods", "methodology", "research_design", "research_designs"),
    "data": ("data", "dataset", "datasets", "data_source", "data_sources"),
    "measure": ("measure", "measures", "measurement", "measurements"),
}

_FACET_PRIORITY = {
    "mechanism": 0,
    "outcome": 1,
    "concept": 2,
    "theory": 3,
    "case": 4,
    "population": 5,
    "geography": 6,
    "method": 7,
    "measure": 8,
    "data": 9,
    "period": 10,
}

_GENERIC_SLUGS = {
    "analysis",
    "analytical",
    "article",
    "book",
    "case",
    "conflict",
    "data",
    "document",
    "full-text",
    "literature",
    "mechanism",
    "mediation",
    "method",
    "none",
    "outcome",
    "paper",
    "peace",
    "research",
    "source",
    "study",
    "theory",
    "unknown",
    "unspecified",
    "war",
}

_BIBLIOGRAPHIC_SLUGS = {
    "bibliography",
    "electronic-book",
    "electronic-books",
    "journal-article",
    "journal-articles",
    "pdf",
    "peer-reviewed",
    "reference",
    "references",
}

_PLURAL_NORMALIZATION = {
    "arrangements": "arrangement",
    "cases": "case",
    "countries": "country",
    "datasets": "dataset",
    "mechanisms": "mechanism",
    "measurements": "measurement",
    "measures": "measure",
    "methods": "method",
    "outcomes": "outcome",
    "participants": "participant",
    "periods": "period",
    "populations": "population",
    "studies": "study",
    "theories": "theory",
    "wars": "war",
}

# Only mechanically equivalent spellings belong here.  Conceptual equivalence
# (for example, impartiality versus neutrality) is deliberately left to the
# reconciliation-proposal audit below.
_SPELLING_NORMALIZATION = {
    "behaviour": "behavior",
    "behaviours": "behaviors",
    "centre": "center",
    "centres": "centers",
    "labour": "labor",
    "modelling": "modeling",
    "organisational": "organizational",
    "organisation": "organization",
    "organisations": "organizations",
}

_RECONCILIATION_FRAME_STOPWORDS = {
    "analysis",
    "case",
    "conflict",
    "data",
    "evidence",
    "literature",
    "mediation",
    "method",
    "outcome",
    "research",
    "study",
    "theory",
}

_RELATION_PRIORITY = {
    "cites": 100,
    "cited_by": 100,
    "zotero_related": 95,
    "same_proposition": 90,
    "shared_concept": 80,
    "same_case": 70,
    "same_outcome": 65,
    "same_method": 60,
    "semantic_similarity": 40,
}

_CITATION_PREDICATES = {
    "cites",
    "citation",
    "citations",
    "references",
    "reference",
    "dc:references",
}
_CITED_BY_PREDICATES = {
    "cited_by",
    "citedby",
    "is_cited_by",
    "isreferencedby",
    "dc:isreferencedby",
}


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _flatten_text(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        for key in ("label", "name", "value", "tag", "text", "title"):
            if key in value:
                return _flatten_text(value[key])
        rows: list[str] = []
        for child in value.values():
            rows.extend(_flatten_text(child))
        return rows
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        rows = []
        for child in value:
            rows.extend(_flatten_text(child))
        return rows
    return [str(value)]


def _clean_label(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip(" \t\r\n,;:.-")


def _subject_slug(value: Any, facet_type: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    if "/" in text and text.split("/", 1)[0].replace("_", "-") == facet_type:
        text = text.split("/", 1)[1]
    text = text.replace("&", " and ").replace("’", "'")
    text = re.sub(r"['`]", "", text)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    tokens = [token for token in text.split("-") if token]
    tokens = [_SPELLING_NORMALIZATION.get(token, token) for token in tokens]
    if tokens and facet_type not in {"geography", "period", "data"}:
        tokens[-1] = _PLURAL_NORMALIZATION.get(tokens[-1], tokens[-1])
    return "-".join(tokens)[:80]


def _invalid_subject_reason(label: str, slug: str) -> str:
    if not label or not slug:
        return "empty"
    if len(label) > 96 or len(slug) > 80:
        return "too_long"
    if re.match(r"^(?:https?|file)://", label, re.I):
        return "url"
    if re.match(r"^10\.\d{4,9}/\S+$", label, re.I):
        return "doi"
    if re.fullmatch(r"(?:isbn(?:-1[03])?:?\s*)?[0-9Xx -]{10,20}", label):
        return "bibliographic_identifier"
    if re.fullmatch(r"\d+(?:\.\d+)?", label):
        return "numeric_only"
    if len(re.findall(r"[A-Za-z0-9]+", label)) > 8 or re.search(r"[?!;]", label):
        return "sentence_like"
    if slug in _BIBLIOGRAPHIC_SLUGS or re.search(r"\.(?:pdf|epub|docx?)$", label, re.I):
        return "bibliographic_format"
    if slug in _GENERIC_SLUGS:
        return "collection_generic"
    return ""


def canonicalize_subject_tag(facet_type: str, value: Any) -> dict[str, Any] | None:
    """Safely canonicalize one typed subject tag without semantic synonym merging."""

    facet = str(facet_type or "").strip().casefold().replace("_", "-")
    if facet not in SUBJECT_FACETS:
        raise ValueError(f"unsupported subject-tag facet: {facet_type!r}")
    label = _clean_label(value)
    slug = _subject_slug(label, facet)
    rejection_reason = _invalid_subject_reason(label, slug)
    if rejection_reason:
        return None
    identity = {"facet_type": facet, "slug": slug}
    return {
        "subject_tag_id": f"subject-tag-{_stable_hash(identity)[:16]}",
        "canonical_tag": f"{facet}/{slug}",
        "label": label,
        "slug": slug,
        "facet_type": facet,
    }


def _profile_values(profile: Mapping[str, Any], facet_type: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    containers = [
        ("profile", profile),
        ("features", profile.get("features")),
        ("dimensions", profile.get("dimensions")),
    ]
    for prefix, container in containers:
        if not isinstance(container, Mapping):
            continue
        for field in _FACET_FIELDS[facet_type]:
            if field not in container:
                continue
            for value in _flatten_text(container.get(field)):
                rows.append((value, f"{prefix}.{field}"))
    if facet_type == "concept" and isinstance(profile.get("semantic_topic_labels"), Mapping):
        for value in _flatten_text(profile["semantic_topic_labels"]):
            rows.append((value, "profile.semantic_topic_labels"))
    return rows


def _source_identity(profile: Mapping[str, Any], position: int) -> tuple[str, str, str]:
    note_id = str(profile.get("note_id") or "").strip()
    source_id = str(profile.get("source_id") or profile.get("id") or note_id).strip()
    if not source_id:
        source_id = f"source-{_stable_hash({'position': position, 'title': profile.get('title', '')})[:12]}"
    if not note_id:
        note_id = f"note-{_stable_hash(source_id)[:12]}"
    family_id = str(profile.get("study_family_id") or profile.get("study_id") or source_id).strip()
    return source_id, note_id, family_id


def _evidence_base_identity(profile: Mapping[str, Any], fallback: str) -> str:
    lineage = profile.get("study_lineage")
    lineage_values = lineage if isinstance(lineage, Mapping) else {}
    return str(
        profile.get("evidence_base_group_id")
        or lineage_values.get("evidence_base_group_id")
        or profile.get("study_family_id")
        or profile.get("study_id")
        or fallback
    ).strip()


def _candidate_rank(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
    slug = str(candidate.get("slug") or "")
    token_count = len(slug.split("-"))
    specificity_penalty = 0 if 1 < token_count <= 5 else 1
    return (
        0 if candidate.get("promotion_status") == "promoted" else 1,
        _FACET_PRIORITY.get(str(candidate.get("facet_type") or ""), 99),
        specificity_penalty,
        -token_count,
        str(candidate.get("canonical_tag") or ""),
    )


def _salient_tag_identifiers(values: Sequence[Any]) -> set[str]:
    identifiers: set[str] = set()
    for value in values:
        if isinstance(value, Mapping):
            for key in ("subject_tag_id", "tag_id", "canonical_tag", "tag", "slug"):
                if value.get(key):
                    identifiers.add(str(value[key]).strip().casefold())
            continue
        rendered = str(value or "").strip().casefold()
        if rendered:
            identifiers.add(rendered)
    return identifiers


def _is_salient_tag(row: Mapping[str, Any], identifiers: set[str]) -> bool:
    return any(
        str(row.get(field) or "").casefold() in identifiers
        for field in ("subject_tag_id", "canonical_tag", "slug")
    )


def _semantic_reconciliation_proposals(
    registries: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Surface plausible semantic reconciliation without silently merging it.

    The rule is deliberately narrow: two multi-token labels in the same facet
    must share an informative lexical frame and differ in at least one token.
    This catches review-worthy pairs such as ``mediator impartiality`` and
    ``mediator neutrality`` while keeping their graph identities separate.
    """

    by_facet: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in registries:
        by_facet[str(row.get("facet_type") or "")].append(row)
    proposals: list[dict[str, Any]] = []
    for facet, rows in sorted(by_facet.items()):
        token_index: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        tokens_by_id: dict[str, set[str]] = {}
        for row in rows:
            tag_id = str(row.get("subject_tag_id") or "")
            tokens = {
                token
                for token in str(row.get("slug") or "").split("-")
                if token and token not in _RECONCILIATION_FRAME_STOPWORDS
            }
            tokens_by_id[tag_id] = tokens
            if len(tokens) < 2:
                continue
            for token in tokens:
                token_index[token].append(row)
        candidate_pairs: set[tuple[str, str]] = set()
        for indexed_rows in token_index.values():
            ids = sorted({str(row.get("subject_tag_id") or "") for row in indexed_rows})
            candidate_pairs.update(combinations(ids, 2))
        rows_by_id = {str(row.get("subject_tag_id") or ""): row for row in rows}
        for left_id, right_id in sorted(candidate_pairs):
            left_tokens = tokens_by_id[left_id]
            right_tokens = tokens_by_id[right_id]
            shared = sorted(left_tokens & right_tokens)
            if not shared:
                continue
            overlap = len(shared) / max(1, min(len(left_tokens), len(right_tokens)))
            if overlap < 0.5 or left_tokens == right_tokens:
                continue
            left = rows_by_id[left_id]
            right = rows_by_id[right_id]
            proposal = {
                "proposal_id": f"tag-reconciliation-proposal-{_stable_hash([left_id, right_id])[:16]}",
                "facet_type": facet,
                "left_subject_tag_id": left_id,
                "right_subject_tag_id": right_id,
                "left_canonical_tag": str(left.get("canonical_tag") or ""),
                "right_canonical_tag": str(right.get("canonical_tag") or ""),
                "shared_lexical_frame": shared,
                "overlap_coefficient": round(overlap, 3),
                "proposed_relation": "related_to",
                "status": "semantic_review_required",
                "automatic_merge": False,
                "reason": "Shared lexical framing is not proof of conceptual equivalence.",
            }
            proposal["revision_hash"] = _stable_hash(proposal)
            proposals.append(proposal)
    return sorted(proposals, key=lambda row: row["proposal_id"])


def derive_subject_tags(
    profiles: Sequence[Mapping[str, Any]],
    *,
    max_candidates_per_source: int = 24,
    max_visible_per_source: int = 8,
    cluster_salient_tags: Sequence[Any] = (),
) -> dict[str, Any]:
    """Derive deterministic collection-native subject tags from committed profiles.

    Zotero tags are promoted only when a typed profile facet confirms the same
    canonical value. Unconfirmed Zotero input remains in the candidate audit and
    cannot leak into native subject tags.
    """

    if max_candidates_per_source < 1:
        raise ValueError("max_candidates_per_source must be positive")
    if max_visible_per_source < 0:
        raise ValueError("max_visible_per_source must be non-negative")

    candidate_rows: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    assignment_rows: list[dict[str, Any]] = []

    for position, profile in enumerate(profiles):
        if not isinstance(profile, Mapping):
            raise ValueError("profiles must contain mappings")
        source_id, note_id, family_id = _source_identity(profile, position)
        by_tag: dict[str, dict[str, Any]] = {}
        for facet_type in SUBJECT_FACETS:
            for raw_value, provenance in _profile_values(profile, facet_type):
                label = _clean_label(raw_value)
                slug = _subject_slug(label, facet_type)
                reason = _invalid_subject_reason(label, slug)
                if reason:
                    rejected.append(
                        {
                            "source_id": source_id,
                            "note_id": note_id,
                            "facet_type": facet_type,
                            "original_value": label,
                            "reason": reason,
                            "provenance": provenance,
                        }
                    )
                    continue
                canonical = canonicalize_subject_tag(facet_type, label)
                if canonical is None:  # guarded above; keeps the public helper independently safe
                    continue
                tag_id = canonical["subject_tag_id"]
                candidate = by_tag.setdefault(
                    tag_id,
                    {
                        **canonical,
                        "source_id": source_id,
                        "note_id": note_id,
                        "study_family_id": family_id,
                        "original_variants": [],
                        "provenance": [],
                        "promotion_status": "promoted",
                    },
                )
                if label not in candidate["original_variants"]:
                    candidate["original_variants"].append(label)
                if provenance not in candidate["provenance"]:
                    candidate["provenance"].append(provenance)

        normalized_zotero_tags = _flatten_text(
            profile.get("normalized_tags")
            or (profile.get("features") or {}).get("zotero_tag_context")
            if isinstance(profile.get("features"), Mapping)
            else profile.get("normalized_tags")
        )
        for raw_value in normalized_zotero_tags:
            label = _clean_label(raw_value)
            matches = [
                row
                for row in by_tag.values()
                if row["slug"] == _subject_slug(label, str(row["facet_type"]))
            ]
            if matches:
                for candidate in matches:
                    if label and label not in candidate["original_variants"]:
                        candidate["original_variants"].append(label)
                    if "zotero.normalized_tags" not in candidate["provenance"]:
                        candidate["provenance"].append("zotero.normalized_tags")
                continue
            slug = _subject_slug(label, "concept")
            reason = _invalid_subject_reason(label, slug)
            if reason:
                rejected.append(
                    {
                        "source_id": source_id,
                        "note_id": note_id,
                        "facet_type": "concept",
                        "original_value": label,
                        "reason": reason,
                        "provenance": "zotero.normalized_tags",
                    }
                )
                continue
            canonical = canonicalize_subject_tag("concept", label)
            if canonical is None:
                continue
            by_tag.setdefault(
                canonical["subject_tag_id"],
                {
                    **canonical,
                    "source_id": source_id,
                    "note_id": note_id,
                    "study_family_id": family_id,
                    "original_variants": [label],
                    "provenance": ["zotero.normalized_tags"],
                    "promotion_status": "unconfirmed_zotero_tag",
                },
            )

        selected = sorted(by_tag.values(), key=_candidate_rank)[:max_candidates_per_source]
        for candidate in selected:
            candidate["original_variants"] = sorted(
                set(candidate["original_variants"]),
                key=lambda value: (str(value).casefold(), str(value)),
            )
            candidate["provenance"] = sorted(set(candidate["provenance"]))
            candidate["label"] = sorted(
                candidate["original_variants"], key=lambda value: (len(value), value.casefold(), value)
            )[0]
            candidate["visible"] = False
            candidate["revision_hash"] = _stable_hash(
                {
                    key: candidate[key]
                    for key in (
                        "subject_tag_id",
                        "source_id",
                        "note_id",
                        "original_variants",
                        "provenance",
                        "promotion_status",
                        "visible",
                    )
                }
            )
            candidate_rows.append(candidate)
            if candidate["promotion_status"] != "promoted":
                continue
            assignment = {
                "assignment_id": f"subject-tag-assignment-{_stable_hash([source_id, candidate['subject_tag_id']])[:16]}",
                "subject_tag_id": candidate["subject_tag_id"],
                "canonical_tag": candidate["canonical_tag"],
                "facet_type": candidate["facet_type"],
                "source_id": source_id,
                "note_id": note_id,
                "study_family_id": family_id,
                "provenance": list(candidate["provenance"]),
                "original_variants": list(candidate["original_variants"]),
                "promotion_status": "promoted",
                "visible": bool(candidate["visible"]),
            }
            assignment["revision_hash"] = _stable_hash(assignment)
            assignment_rows.append(assignment)

    # A source-local profile facet remains queryable in the audit, but it does
    # not become a native graph tag merely because it was extracted once.  A
    # graph-active tag needs two analytical evidence bases or an explicit
    # proposition/cluster salience marker.
    by_source, _ = _profile_index(profiles)
    eligible_families_by_tag: dict[str, set[str]] = defaultdict(set)
    for assignment in assignment_rows:
        source_id = str(assignment["source_id"])
        profile = by_source.get(source_id, {})
        if not _profile_is_analytical(profile):
            continue
        eligible_families_by_tag[str(assignment["subject_tag_id"])].add(
            _evidence_base_identity(profile, source_id)
        )
    salient_identifiers = _salient_tag_identifiers(cluster_salient_tags)
    active_tag_ids = {
        str(assignment["subject_tag_id"])
        for assignment in assignment_rows
        if len(eligible_families_by_tag[str(assignment["subject_tag_id"])]) >= 2
        or _is_salient_tag(assignment, salient_identifiers)
    }
    candidates_by_assignment = {
        (str(row["source_id"]), str(row["subject_tag_id"])): row
        for row in candidate_rows
        if row.get("promotion_status") != "unconfirmed_zotero_tag"
    }
    active_candidates_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for assignment in assignment_rows:
        key = (str(assignment["source_id"]), str(assignment["subject_tag_id"]))
        candidate = candidates_by_assignment[key]
        active = str(assignment["subject_tag_id"]) in active_tag_ids
        promotion_status = "promoted" if active else "source_local_only"
        assignment["promotion_status"] = promotion_status
        assignment["visible"] = False
        candidate["promotion_status"] = promotion_status
        candidate["visible"] = False
        if active:
            active_candidates_by_source[str(assignment["source_id"])].append(candidate)
    visible_pairs: set[tuple[str, str]] = set()
    for source_id, candidates in active_candidates_by_source.items():
        for candidate in sorted(candidates, key=_candidate_rank)[:max_visible_per_source]:
            visible_pairs.add((source_id, str(candidate["subject_tag_id"])))
    for assignment in assignment_rows:
        key = (str(assignment["source_id"]), str(assignment["subject_tag_id"]))
        assignment["visible"] = key in visible_pairs
        assignment["revision_hash"] = _stable_hash(
            {field: value for field, value in assignment.items() if field != "revision_hash"}
        )
        candidate = candidates_by_assignment[key]
        candidate["visible"] = assignment["visible"]
        candidate["revision_hash"] = _stable_hash(
            {field: value for field, value in candidate.items() if field != "revision_hash"}
        )

    registry_by_id: dict[str, dict[str, Any]] = {}
    for assignment in assignment_rows:
        tag_id = assignment["subject_tag_id"]
        candidate = next(row for row in candidate_rows if row["subject_tag_id"] == tag_id)
        registry = registry_by_id.setdefault(
            tag_id,
            {
                "subject_tag_id": tag_id,
                "canonical_tag": assignment["canonical_tag"],
                "label": candidate["label"],
                "slug": candidate["slug"],
                "facet_type": assignment["facet_type"],
                "original_variants": [],
                "source_ids": [],
                "note_ids": [],
                "study_family_ids": [],
                "assignment_provenance": [],
                "reconciliation_status": "canonical",
            },
        )
        registry["original_variants"].extend(assignment["original_variants"])
        registry["source_ids"].append(assignment["source_id"])
        registry["note_ids"].append(assignment["note_id"])
        registry["study_family_ids"].append(assignment["study_family_id"])
        registry["assignment_provenance"].extend(assignment["provenance"])

    registries = []
    for registry in registry_by_id.values():
        for field in (
            "original_variants",
            "source_ids",
            "note_ids",
            "study_family_ids",
            "assignment_provenance",
        ):
            registry[field] = sorted(
                set(registry[field]),
                key=lambda value: (str(value).casefold(), str(value)),
            )
        registry["label"] = sorted(
            registry["original_variants"], key=lambda value: (len(value), value.casefold(), value)
        )[0]
        registry["source_count"] = len(registry["source_ids"])
        registry["study_family_count"] = len(registry["study_family_ids"])
        registry["eligible_study_family_count"] = len(
            eligible_families_by_tag.get(str(registry["subject_tag_id"]), set())
        )
        registry["effective_evidence_base_count"] = registry["eligible_study_family_count"]
        registry["graph_active"] = str(registry["subject_tag_id"]) in active_tag_ids
        registry["promotion_status"] = (
            "promoted" if registry["graph_active"] else "source_local_only"
        )
        registry["activation_reason"] = (
            "explicit_cluster_salience"
            if registry["graph_active"]
            and _is_salient_tag(registry, salient_identifiers)
            and registry["eligible_study_family_count"] < 2
            else "repeated_analytical_evidence_bases"
            if registry["graph_active"]
            else "source_local_singleton"
        )
        registry["tag_concept_id"] = f"tag-concept-{_stable_hash([registry['facet_type'], registry['slug']])[:16]}"
        registry["revision_hash"] = _stable_hash(registry)
        registries.append(registry)

    alias_relations: list[dict[str, Any]] = []
    for registry in registries:
        canonical_label = str(registry["label"])
        for variant in registry["original_variants"]:
            if variant == canonical_label:
                continue
            relation = {
                "tag_relation_id": f"tag-relation-{_stable_hash([registry['tag_concept_id'], variant])[:16]}",
                "relation_type": "alias_of",
                "alias": str(variant),
                "canonical_label": canonical_label,
                "tag_concept_id": registry["tag_concept_id"],
                "subject_tag_id": registry["subject_tag_id"],
                "automatic": True,
                "basis": "safe_lexical_normalization",
            }
            relation["revision_hash"] = _stable_hash(relation)
            alias_relations.append(relation)
    reconciliation_proposals = _semantic_reconciliation_proposals(registries)
    concept_id_by_subject_tag = {
        str(row["subject_tag_id"]): str(row["tag_concept_id"]) for row in registries
    }
    semantic_relations_by_concept: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for proposal in reconciliation_proposals:
        left_concept = concept_id_by_subject_tag[str(proposal["left_subject_tag_id"])]
        right_concept = concept_id_by_subject_tag[str(proposal["right_subject_tag_id"])]
        semantic_relations_by_concept[left_concept].append(
            {
                "relation_type": "related_to",
                "target_tag_concept_id": right_concept,
                "status": "semantic_review_required",
                "automatic_merge": False,
                "proposal_id": proposal["proposal_id"],
            }
        )
        semantic_relations_by_concept[right_concept].append(
            {
                "relation_type": "related_to",
                "target_tag_concept_id": left_concept,
                "status": "semantic_review_required",
                "automatic_merge": False,
                "proposal_id": proposal["proposal_id"],
            }
        )
    tag_concepts: list[dict[str, Any]] = []
    for registry in registries:
        concept_id = str(registry["tag_concept_id"])
        concept = {
            "tag_concept_id": concept_id,
            "label": str(registry["label"]),
            "slug": str(registry["slug"]),
            "original_variants": list(registry["original_variants"]),
            "source_ids": list(registry["source_ids"]),
            "relations": sorted(
                semantic_relations_by_concept.get(concept_id, []),
                key=lambda row: (str(row["target_tag_concept_id"]), str(row["proposal_id"])),
            ),
            "graph_active": bool(registry["graph_active"]),
            "activation_reason": str(registry["activation_reason"]),
        }
        concept["revision_hash"] = _stable_hash(concept)
        tag_concepts.append(concept)
    fragmented_ids = {
        str(row[field])
        for row in reconciliation_proposals
        for field in ("left_subject_tag_id", "right_subject_tag_id")
    }
    active_count = sum(bool(row["graph_active"]) for row in registries)
    inactive_count = len(registries) - active_count
    navigation_metrics = {
        "candidate_tag_count": len(candidate_rows),
        "canonical_tag_concept_count": len(registries),
        "active_tag_concept_count": active_count,
        "inactive_source_local_tag_count": inactive_count,
        "source_local_singleton_tag_count": sum(
            row["activation_reason"] == "source_local_singleton" for row in registries
        ),
        "fragmented_tag_concept_count": len(fragmented_ids),
        "unresolved_reconciliation_count": len(reconciliation_proposals),
        "safe_alias_count": len(alias_relations),
        "active_vocabulary_ratio": round(active_count / max(1, len(registries)), 4),
    }

    return {
        "tag_reconciliation_version": TAG_RECONCILIATION_VERSION,
        "subject_tags": sorted(registries, key=lambda row: row["subject_tag_id"]),
        "active_subject_tags": sorted(
            (row for row in registries if row["graph_active"]),
            key=lambda row: row["subject_tag_id"],
        ),
        "tag_concept_registry": sorted(tag_concepts, key=lambda row: row["tag_concept_id"]),
        "tag_concept_relations": sorted(alias_relations, key=lambda row: row["tag_relation_id"]),
        "tag_reconciliation_proposals": reconciliation_proposals,
        "assignments": sorted(assignment_rows, key=lambda row: row["assignment_id"]),
        "candidates": sorted(candidate_rows, key=lambda row: (row["source_id"], _candidate_rank(row))),
        "rejected_candidates": sorted(
            rejected,
            key=lambda row: (row["source_id"], row["facet_type"], row["original_value"].casefold()),
        ),
        "candidate_count": len(candidate_rows),
        "promoted_subject_tag_count": active_count,
        "source_local_subject_tag_count": inactive_count,
        "rejected_generic_tag_count": sum(row["reason"] == "collection_generic" for row in rejected),
        "unconfirmed_zotero_tag_count": sum(
            row["promotion_status"] == "unconfirmed_zotero_tag" for row in candidate_rows
        ),
        "navigation_metrics": navigation_metrics,
    }


def _profile_index(profiles: Sequence[Mapping[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    by_source: dict[str, dict[str, Any]] = {}
    aliases: dict[str, str] = {}
    for position, profile in enumerate(profiles):
        if not isinstance(profile, Mapping):
            raise ValueError("profiles must contain mappings")
        source_id, note_id, family_id = _source_identity(profile, position)
        row = dict(profile)
        row.update({"source_id": source_id, "note_id": note_id, "study_family_id": family_id})
        by_source[source_id] = row
        for alias in (
            source_id,
            note_id,
            str(profile.get("zotero_item_key") or ""),
            str(profile.get("item_key") or ""),
        ):
            if alias:
                aliases[alias.casefold()] = source_id
    return by_source, aliases


def _relation_target(value: Any) -> str:
    if isinstance(value, Mapping):
        for key in ("target_source_id", "source_id", "target_note_id", "note_id", "target", "value", "key", "uri"):
            if value.get(key):
                return _relation_target(value[key])
        return ""
    target = str(value or "").strip().rstrip("/")
    return target.rsplit("/", 1)[-1] if "/" in target else target


def _relation_values(value: Any, default_predicate: str) -> list[tuple[str, str]]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        if any(key in value for key in ("target", "target_source_id", "target_note_id")):
            predicate = str(value.get("predicate") or value.get("relation_type") or default_predicate)
            return [(predicate, _relation_target(value))]
        rows: list[tuple[str, str]] = []
        for predicate, children in value.items():
            for target in _flatten_text(children):
                rows.append((str(predicate), _relation_target(target)))
        return rows
    return [(default_predicate, _relation_target(target)) for target in _flatten_text(value)]


def _classified_relation(predicate: str) -> str:
    normalized = re.sub(r"[\s-]+", "_", predicate.strip().casefold())
    compact = normalized.replace("_", "")
    if normalized in _CITATION_PREDICATES or compact in {"cites", "references"}:
        return "cites"
    if normalized in _CITED_BY_PREDICATES or compact in {"citedby", "isreferencedby"}:
        return "cited_by"
    return "zotero_related"


def _relation_record(
    source_id: str,
    target_id: str,
    relation_type: str,
    by_source: Mapping[str, Mapping[str, Any]],
    *,
    evidence: Sequence[Mapping[str, Any]],
    provenance: str,
    inferred: bool,
) -> dict[str, Any]:
    symmetric = relation_type not in {"cites", "cited_by"}
    left, right = (sorted((source_id, target_id)) if symmetric else (source_id, target_id))
    return {
        "relation_id": f"typed-relation-{_stable_hash([left, right, relation_type])[:16]}",
        "source_id": left,
        "target_source_id": right,
        "source_note_id": str(by_source[left].get("note_id") or ""),
        "target_note_id": str(by_source[right].get("note_id") or ""),
        "relation_type": relation_type,
        "evidence": [dict(row) for row in evidence],
        "provenance": provenance,
        "inferred": inferred,
        "strength": _RELATION_PRIORITY[relation_type],
    }


def build_typed_source_relations(
    profiles: Sequence[Mapping[str, Any]],
    *,
    tag_assignments: Sequence[Mapping[str, Any]] = (),
    propositions: Sequence[Mapping[str, Any]] = (),
    max_inferred_links_per_source: int = 8,
) -> list[dict[str, Any]]:
    """Build explicit and inferred relations without conflating citations and similarity."""

    if max_inferred_links_per_source < 0:
        raise ValueError("max_inferred_links_per_source must be non-negative")
    by_source, aliases = _profile_index(profiles)
    explicit: dict[str, dict[str, Any]] = {}
    inferred: dict[str, dict[str, Any]] = {}

    def add(record: dict[str, Any], target: dict[str, dict[str, Any]]) -> None:
        existing = target.get(record["relation_id"])
        if existing is None:
            target[record["relation_id"]] = record
            return
        evidence = {json.dumps(row, sort_keys=True): row for row in existing["evidence"]}
        evidence.update({json.dumps(row, sort_keys=True): row for row in record["evidence"]})
        existing["evidence"] = [evidence[key] for key in sorted(evidence)]

    for source_id, profile in by_source.items():
        relation_fields = (
            ("citation_relations", "cites", "exact_source_relation"),
            ("zotero_relations", "zotero_related", "exact_source_relation"),
            ("relations", "zotero_related", "exact_source_relation"),
            ("custody_relations", "zotero_related", "01_custody/source_relation_registry.csv"),
        )
        for field, default_predicate, provenance in relation_fields:
            for predicate, target_alias in _relation_values(profile.get(field), default_predicate):
                target_id = aliases.get(target_alias.casefold())
                if not target_id or target_id == source_id:
                    continue
                relation_type = _classified_relation(predicate)
                record = _relation_record(
                    source_id,
                    target_id,
                    relation_type,
                    by_source,
                    evidence=[{"predicate": predicate, "target": target_alias}],
                    provenance=provenance,
                    inferred=False,
                )
                add(record, explicit)
                if relation_type in {"cites", "cited_by"}:
                    inverse = "cited_by" if relation_type == "cites" else "cites"
                    add(
                        _relation_record(
                            target_id,
                            source_id,
                            inverse,
                            by_source,
                            evidence=[{"inverse_of": record["relation_id"]}],
                            provenance="inverse_exact_citation",
                            inferred=False,
                        ),
                        explicit,
                    )

    for proposition in propositions:
        proposition_id = str(proposition.get("proposition_id") or "")
        source_ids = sorted(
            {
                str(source_id)
                for source_id in proposition.get("source_ids", []) or []
                if str(source_id) in by_source
            }
        )
        for left, right in combinations(source_ids, 2):
            add(
                _relation_record(
                    left,
                    right,
                    "same_proposition",
                    by_source,
                    evidence=[
                        {
                            "proposition_id": proposition_id,
                            "statement": str(proposition.get("statement") or ""),
                        }
                    ],
                    provenance="admitted_literature_proposition",
                    inferred=True,
                ),
                inferred,
            )

    assignments_by_source: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    labels_by_tag: dict[str, str] = {}
    for assignment in tag_assignments:
        if assignment.get("promotion_status", "promoted") != "promoted":
            continue
        source_id = str(assignment.get("source_id") or "")
        facet = str(assignment.get("facet_type") or "")
        tag_id = str(assignment.get("subject_tag_id") or "")
        canonical_tag = str(assignment.get("canonical_tag") or "")
        if source_id not in by_source or facet not in SUBJECT_FACETS or not tag_id:
            continue
        slug = canonical_tag.split("/", 1)[-1]
        if slug in _GENERIC_SLUGS:
            continue
        assignments_by_source[source_id][facet].add(tag_id)
        labels_by_tag[tag_id] = canonical_tag

    source_ids = sorted(by_source)
    for left, right in combinations(source_ids, 2):
        left_facets = assignments_by_source[left]
        right_facets = assignments_by_source[right]
        shared = {
            facet: sorted(left_facets.get(facet, set()) & right_facets.get(facet, set()))
            for facet in SUBJECT_FACETS
        }
        shared = {facet: tag_ids for facet, tag_ids in shared.items() if tag_ids}
        shared_all = sorted({tag_id for tag_ids in shared.values() for tag_id in tag_ids})
        # A lone shared case, method, outcome, or concept is a useful
        # neighborhood path, not enough evidence for a direct source-to-source
        # edge.  Same-proposition links were already admitted above; all other
        # inferred direct links require overlap in two distinct typed facets.
        if len(shared) < 2:
            continue
        facet_relations = {"case": "same_case", "method": "same_method", "outcome": "same_outcome"}
        for facet, relation_type in facet_relations.items():
            if not shared.get(facet):
                continue
            add(
                _relation_record(
                    left,
                    right,
                    relation_type,
                    by_source,
                    evidence=[{"subject_tags": [labels_by_tag[tag_id] for tag_id in shared[facet]]}],
                    provenance="canonical_subject_tag_overlap",
                    inferred=True,
                ),
                inferred,
            )
        if shared.get("concept"):
            add(
                _relation_record(
                    left,
                    right,
                    "shared_concept",
                    by_source,
                    evidence=[
                        {
                            "subject_tags": [labels_by_tag[tag_id] for tag_id in shared["concept"]],
                            "supporting_subject_tags": [labels_by_tag[tag_id] for tag_id in shared_all],
                        }
                    ],
                    provenance="multiple_specific_subject_facets",
                    inferred=True,
                ),
                inferred,
            )
        if shared_all:
            overlap = len(shared_all) / max(
                1,
                min(
                    sum(len(values) for values in left_facets.values()),
                    sum(len(values) for values in right_facets.values()),
                ),
            )
            add(
                _relation_record(
                    left,
                    right,
                    "semantic_similarity",
                    by_source,
                    evidence=[
                        {
                            "shared_subject_tags": [labels_by_tag[tag_id] for tag_id in shared_all],
                            "overlap_coefficient": round(overlap, 3),
                        }
                    ],
                    provenance="typed_profile_similarity",
                    inferred=True,
                ),
                inferred,
            )

    inferred_rows = sorted(
        inferred.values(),
        key=lambda row: (-int(row["strength"]), row["source_id"], row["target_source_id"], row["relation_id"]),
    )
    admitted_neighbors: dict[str, set[str]] = defaultdict(set)
    admitted_pairs: set[tuple[str, str]] = set()
    bounded: list[dict[str, Any]] = []
    for row in inferred_rows:
        pair = tuple(sorted((row["source_id"], row["target_source_id"])))
        left, right = pair
        if pair not in admitted_pairs:
            if (
                len(admitted_neighbors[left]) >= max_inferred_links_per_source
                or len(admitted_neighbors[right]) >= max_inferred_links_per_source
            ):
                continue
            admitted_pairs.add(pair)
            admitted_neighbors[left].add(right)
            admitted_neighbors[right].add(left)
        bounded.append(row)

    result = [*explicit.values(), *bounded]
    for row in result:
        row["revision_hash"] = _stable_hash(row)
    return sorted(result, key=lambda row: row["relation_id"])


def _profile_is_analytical(profile: Mapping[str, Any]) -> bool:
    if profile.get("excluded_from_synthesis"):
        return False
    if profile.get("analytical") is not None:
        return bool(profile.get("analytical"))
    status = str(profile.get("note_status") or profile.get("profile_status") or profile.get("status") or "analytical")
    return status.casefold() not in {
        "partial_document_atomic_note",
        "abstract_only_atomic_note",
        "metadata_only_source_note",
        "fulltext_available",
        "limited",
        "limited_context_only",
    }


def promote_topic_neighborhoods(
    profiles: Sequence[Mapping[str, Any]],
    tag_registry: Sequence[Mapping[str, Any]],
    tag_assignments: Sequence[Mapping[str, Any]],
    *,
    minimum_independent_sources: int = 2,
    max_visible_collection_neighborhoods: int = 20,
) -> dict[str, Any]:
    """Promote only genuinely multi-source subject facets into neighborhoods."""

    if minimum_independent_sources < 2:
        raise ValueError("minimum_independent_sources must be at least two")
    if max_visible_collection_neighborhoods < 0:
        raise ValueError("max_visible_collection_neighborhoods must be non-negative")
    by_source, _ = _profile_index(profiles)
    analytical_families = {
        _evidence_base_identity(row, source_id)
        for source_id, row in by_source.items()
        if _profile_is_analytical(row)
    }
    registry = {str(row.get("subject_tag_id") or ""): dict(row) for row in tag_registry}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for assignment in tag_assignments:
        if assignment.get("promotion_status") == "unconfirmed_zotero_tag":
            continue
        tag_id = str(assignment.get("subject_tag_id") or "")
        source_id = str(assignment.get("source_id") or "")
        if tag_id in registry and source_id in by_source:
            grouped[tag_id].append(dict(assignment))

    neighborhoods: list[dict[str, Any]] = []
    singletons: list[dict[str, Any]] = []
    for tag_id, assignments in sorted(grouped.items()):
        tag = registry[tag_id]
        source_ids = sorted({str(row["source_id"]) for row in assignments})
        eligible_source_ids = [source_id for source_id in source_ids if _profile_is_analytical(by_source[source_id])]
        eligible_families = {
            _evidence_base_identity(by_source[source_id], source_id) for source_id in eligible_source_ids
        }
        graph_active = bool(tag.get("graph_active")) or any(
            row.get("promotion_status", "promoted") == "promoted" for row in assignments
        )
        if (
            not graph_active
            or len(eligible_source_ids) < minimum_independent_sources
            or len(eligible_families) < minimum_independent_sources
        ):
            singletons.append(
                {
                    "subject_tag_id": tag_id,
                    "canonical_tag": tag.get("canonical_tag", ""),
                    "source_ids": source_ids,
                    "eligible_source_ids": eligible_source_ids,
                    "reason": (
                        "duplicate_study_family_only"
                        if len(eligible_source_ids) >= minimum_independent_sources
                        else "fewer_than_two_eligible_sources"
                    ),
                    "graph_active": graph_active,
                    "audit_status": (
                        "graph_active_without_neighborhood" if graph_active else "source_local_only"
                    ),
                }
            )
            continue
        note_ids = sorted({str(by_source[source_id].get("note_id") or "") for source_id in source_ids})
        member_reasons = []
        for source_id in source_ids:
            member_reasons.append(
                {
                    "source_id": source_id,
                    "note_id": str(by_source[source_id].get("note_id") or ""),
                    "reason": f"Shared {tag.get('facet_type', 'subject')}: {tag.get('label') or tag.get('canonical_tag')}",
                    "context_only": not _profile_is_analytical(by_source[source_id]),
                }
            )
        semantic_identity = str(tag.get("slug") or str(tag.get("canonical_tag") or "").split("/", 1)[-1])
        prevalence = len(eligible_families) / max(1, len(analytical_families))
        specificity = min(5, len([token for token in semantic_identity.split("-") if token]))
        facet_bonus = max(0, 11 - _FACET_PRIORITY.get(str(tag.get("facet_type") or ""), 11))
        usefulness_score = round(
            ((1.0 - prevalence) * 50) + (specificity * 8) + facet_bonus + min(5, len(eligible_families)) * 2,
            3,
        )
        representative_sources = [
            {
                "source_id": source_id,
                "note_id": str(by_source[source_id].get("note_id") or ""),
                "title": str(by_source[source_id].get("title") or source_id),
            }
            for source_id in eligible_source_ids[:5]
        ]
        neighborhood = {
            "topic_neighborhood_id": f"topic-neighborhood-{_stable_hash(tag_id)[:16]}",
            "facet_type": str(tag.get("facet_type") or ""),
            "canonical_tag_id": tag_id,
            "semantic_identity": semantic_identity,
            "label": str(tag.get("label") or tag.get("canonical_tag") or ""),
            "source_ids": source_ids,
            "note_ids": note_ids,
            "source_count": len(source_ids),
            "independent_source_count": len(eligible_families),
            "study_family_count": len(eligible_families),
            "effective_evidence_base_count": len(eligible_families),
            "collection_evidence_base_count": len(analytical_families),
            "collection_prevalence": round(prevalence, 4),
            "discriminative_usefulness_score": usefulness_score,
            "representative_sources": representative_sources,
            "why_useful": (
                f"Connects {len(eligible_families)} independent evidence bases through shared "
                f"{tag.get('facet_type', 'subject')} evidence on {tag.get('label') or semantic_identity}."
            ),
            "member_relationship_reasons": member_reasons,
            "promotion_status": "promoted",
            "visibility_status": "visible",
            "analytical_support": False,
            "navigation_role": "retrieval_only",
        }
        neighborhood["revision_hash"] = _stable_hash(neighborhood)
        neighborhoods.append(neighborhood)

    ranked_neighborhoods = sorted(
        neighborhoods,
        key=lambda row: (
            -float(row["discriminative_usefulness_score"]),
            -int(row["independent_source_count"]),
            str(row["topic_neighborhood_id"]),
        ),
    )
    human_summaries = [
        {
            "neighborhood_id": row["topic_neighborhood_id"],
            "label": row["label"],
            "why_useful": row["why_useful"],
            "source_ids": row["source_ids"],
            "effective_evidence_base_count": row["effective_evidence_base_count"],
            "related_cluster_ids": [],
            "representative_source_ids": [
                source["source_id"] for source in row["representative_sources"]
            ],
            "relationship_reasons": [
                str(reason["reason"]) for reason in row["member_relationship_reasons"]
            ],
            "visible": True,
        }
        for row in ranked_neighborhoods[:max_visible_collection_neighborhoods]
    ]
    return {
        "neighborhood_promotion_version": NEIGHBORHOOD_PROMOTION_VERSION,
        "topic_neighborhoods": sorted(neighborhoods, key=lambda row: row["topic_neighborhood_id"]),
        "human_neighborhood_summaries": human_summaries,
        "singleton_facets": sorted(singletons, key=lambda row: row["subject_tag_id"]),
        "promoted_neighborhood_count": len(neighborhoods),
        "singleton_facet_count": len(singletons),
    }


def rank_topic_neighborhoods(
    neighborhoods: Sequence[Mapping[str, Any]],
    source_ids: Sequence[str],
    *,
    proposition_tag_ids: Sequence[str] = (),
    max_visible: int = 8,
) -> list[dict[str, Any]]:
    """Rank cluster-relevant neighborhoods without exposing machine-only singletons."""

    if max_visible < 0:
        raise ValueError("max_visible must be non-negative")
    members = set(source_ids)
    proposition_tags = set(proposition_tag_ids)
    eligible = []
    for neighborhood in neighborhoods:
        overlap = len(members & set(neighborhood.get("source_ids", []) or []))
        if overlap < 2 or neighborhood.get("promotion_status", "promoted") != "promoted":
            continue
        row = dict(neighborhood)
        row["cluster_member_count"] = overlap
        row["proposition_relevant"] = str(row.get("canonical_tag_id") or "") in proposition_tags
        eligible.append(row)
    return sorted(
        eligible,
        key=lambda row: (
            0 if row["proposition_relevant"] else 1,
            -float(row.get("discriminative_usefulness_score") or 0),
            -row["cluster_member_count"],
            -len(str(row.get("semantic_identity") or "").split("-")),
            str(row.get("topic_neighborhood_id") or ""),
        ),
    )[:max_visible]


def _relation_reason(relation_type: str, evidence: Sequence[Mapping[str, Any]]) -> str:
    first = evidence[0] if evidence else {}
    if relation_type == "cites":
        return "Citation: this source cites the related source."
    if relation_type == "cited_by":
        return "Citation: this source is cited by the related source."
    if relation_type == "zotero_related":
        return "Explicitly related in Zotero."
    if relation_type == "same_proposition":
        statement = str(first.get("statement") or "").strip()
        return f"Addresses the same proposition: {statement}" if statement else "Addresses the same proposition."
    labels = first.get("subject_tags") or first.get("shared_subject_tags") or []
    human_labels: list[str] = []
    seen_labels: set[str] = set()
    for label in labels:
        human_label = str(label).split("/", 1)[-1].replace("-", " ")
        identity = human_label.casefold()
        if identity in seen_labels:
            continue
        seen_labels.add(identity)
        human_labels.append(human_label)
    rendered = ", ".join(human_labels[:3])
    prefix = {
        "shared_concept": "Shared concept" if len(human_labels) == 1 else "Shared concepts",
        "same_case": "Same case",
        "same_method": "Same method",
        "same_outcome": "Same outcome",
        "semantic_similarity": "Related profile evidence",
    }.get(relation_type, "Related evidence")
    return f"{prefix}: {rendered}." if rendered else f"{prefix}."


def rank_human_related_links(
    source_id: str,
    profiles: Sequence[Mapping[str, Any]],
    relations: Sequence[Mapping[str, Any]],
    *,
    max_inferred_links: int = 8,
) -> list[dict[str, Any]]:
    """Return bounded, explained related-source links for one human projection."""

    if max_inferred_links < 0:
        raise ValueError("max_inferred_links must be non-negative")
    by_source, _ = _profile_index(profiles)
    if source_id not in by_source:
        return []
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for relation in relations:
        left = str(relation.get("source_id") or "")
        right = str(relation.get("target_source_id") or "")
        if source_id == left:
            target = right
        elif source_id == right and relation.get("relation_type") not in {"cites", "cited_by"}:
            target = left
        else:
            continue
        if target in by_source:
            grouped[target].append(dict(relation))

    explicit_targets = {
        target for target, rows in grouped.items() if any(not bool(row.get("inferred")) for row in rows)
    }
    inferred_targets = sorted(
        (target for target in grouped if target not in explicit_targets),
        key=lambda target: (
            -max(int(row.get("strength") or 0) for row in grouped[target]),
            target,
        ),
    )[:max_inferred_links]
    selected = sorted(explicit_targets) + inferred_targets
    rendered = []
    for target in selected:
        rows = sorted(grouped[target], key=lambda row: (-int(row.get("strength") or 0), row["relation_id"]))
        primary = rows[0]
        profile = by_source[target]
        rendered.append(
            {
                "relation_id": str(primary.get("relation_id") or ""),
                "target_source_id": target,
                "target_note_id": str(profile.get("note_id") or ""),
                "target_title": str(profile.get("title") or target),
                "relation_types": sorted({str(row.get("relation_type") or "") for row in rows}),
                "primary_relation_type": str(primary.get("relation_type") or ""),
                "reason": _relation_reason(str(primary.get("relation_type") or ""), primary.get("evidence", []) or []),
                "explicit": any(not bool(row.get("inferred")) for row in rows),
                "strength": int(primary.get("strength") or 0),
            }
        )
    return sorted(rendered, key=lambda row: (-row["strength"], row["target_source_id"]))


def _proposition_salient_tags(propositions: Sequence[Mapping[str, Any]]) -> list[Any]:
    values: list[Any] = []
    for proposition in propositions:
        for field in (
            "subject_tag_ids",
            "subject_tags",
            "cluster_salient_tags",
            "canonical_tags",
        ):
            raw = proposition.get(field)
            if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
                values.extend(raw)
            elif raw:
                values.append(raw)
    return values


def build_navigation_graph(
    profiles: Sequence[Mapping[str, Any]],
    *,
    propositions: Sequence[Mapping[str, Any]] = (),
    max_candidates_per_source: int = 24,
    max_visible_tags_per_source: int = 8,
    max_inferred_links_per_source: int = 8,
    minimum_neighborhood_sources: int = 2,
    max_visible_collection_neighborhoods: int = 20,
) -> dict[str, Any]:
    """Build the complete deterministic tag and navigation projection with no model calls."""

    tags = derive_subject_tags(
        profiles,
        max_candidates_per_source=max_candidates_per_source,
        max_visible_per_source=max_visible_tags_per_source,
        cluster_salient_tags=_proposition_salient_tags(propositions),
    )
    relations = build_typed_source_relations(
        profiles,
        tag_assignments=tags["assignments"],
        propositions=propositions,
        max_inferred_links_per_source=max_inferred_links_per_source,
    )
    neighborhoods = promote_topic_neighborhoods(
        profiles,
        tags["subject_tags"],
        tags["assignments"],
        minimum_independent_sources=minimum_neighborhood_sources,
        max_visible_collection_neighborhoods=max_visible_collection_neighborhoods,
    )
    relation_counts = {
        relation_type: sum(row["relation_type"] == relation_type for row in relations)
        for relation_type in sorted(TYPED_SOURCE_RELATIONS)
    }
    graph_projection_hash = _stable_hash(
        {
            "tag_reconciliation_version": TAG_RECONCILIATION_VERSION,
            "navigation_relation_version": NAVIGATION_RELATION_VERSION,
            "neighborhood_promotion_version": NEIGHBORHOOD_PROMOTION_VERSION,
            "subject_tags": tags["subject_tags"],
            "assignments": tags["assignments"],
            "tag_concept_relations": tags["tag_concept_relations"],
            "tag_reconciliation_proposals": tags["tag_reconciliation_proposals"],
            "typed_relations": relations,
            "topic_neighborhoods": neighborhoods["topic_neighborhoods"],
            "human_neighborhood_summaries": neighborhoods["human_neighborhood_summaries"],
        }
    )
    navigation_metrics = {
        **tags["navigation_metrics"],
        "promoted_neighborhood_count": neighborhoods["promoted_neighborhood_count"],
        "singleton_facet_count": neighborhoods["singleton_facet_count"],
        "human_visible_neighborhood_count": len(neighborhoods["human_neighborhood_summaries"]),
        "inferred_direct_relation_count": sum(bool(row.get("inferred")) for row in relations),
        "explicit_relation_count": sum(not bool(row.get("inferred")) for row in relations),
    }
    return {
        **tags,
        **neighborhoods,
        "navigation_relation_version": NAVIGATION_RELATION_VERSION,
        "typed_relations": relations,
        "typed_relation_counts": relation_counts,
        "navigation_metrics": navigation_metrics,
        "graph_projection_hash": graph_projection_hash,
    }
