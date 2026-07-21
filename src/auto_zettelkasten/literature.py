from __future__ import annotations

import ast
import csv
import inspect
import json
import re
import time
from collections import Counter, defaultdict
from dataclasses import asdict, is_dataclass
from itertools import combinations
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

from .files import (
    atomic_write_text,
    now_iso,
    read_yaml,
    safe_filename,
    sha256_text,
    slugify,
    write_yaml,
)

from .models import FAMILY_RELATION_TYPES
from .notes import source_note_semantic_components

from .navigation import (
    build_navigation_graph,
    build_typed_source_relations,
    rank_topic_neighborhoods,
)


ANALYTICAL_STATUSES = {"analytical_atomic_note", "verified_atomic_note", "analytical"}
LIMITED_STATUSES = {
    "abstract_only_atomic_note",
    "metadata_only_source_note",
    "fulltext_available",
    "limited",
}
EVIDENCE_DIMENSIONS = (
    "theory",
    "mechanism",
    "method",
    "data",
    "case",
    "period",
    "outcome",
    "finding direction",
    "uncertainty",
    "limitations",
)
GAP_RULES = (
    "contradictory_findings",
    "untested_mechanism",
    "empirical_coverage",
    "methodological_concentration",
    "measurement_or_data",
    "boundary_condition",
    "replication",
    "cross_cluster_integration",
    "author_stated_gap",
)
LITERATURE_ALGORITHM_VERSION = "29"
CLUSTER_PROPOSAL_PROMPT_VERSION = "17"
CLUSTER_SYNTHESIS_PROMPT_VERSION = "14"
GAP_REASONING_PROMPT_VERSION = "10"
ANCHOR_ALGORITHM_VERSION = "3"
SUPPORT_ENVELOPE_VERSION = "2"
PROPOSITION_ALGORITHM_VERSION = "14"
PROPOSITION_MATRIX_VERSION = "3"
GAP_RULE_VERSION = "3"

FAMILY_RELATION_VERSION = "6"

FAMILY_ADMISSION_VERSION = "9"

STRICT_ADJUDICATION_VERSION = "3"

STUDY_LINEAGE_VERSION = "2"
INDEPENDENCE_ALGORITHM_VERSION = "2"
QUANTITATIVE_VALIDATION_VERSION = "2"
LOCATOR_AUDIT_VERSION = "2"

DEBATE_STATES = {
    "mapped_debate",
    "mapped_consensus",
    "emerging_convergence",
    "aligned_institutional_guidance",
    "within_program_consistency",
    "mixed_evidence",
    "conditional_relationship",
    "complementary_positions",
    "parallel_literatures",
    "single_position",
    "no_debate",
}

CAUSAL_SUPPORT_ROLES = {"causal"}
EMPIRICAL_SUPPORT_ROLES = {
    "descriptive",
    "associational",
    "mechanism_evidence",
    *CAUSAL_SUPPORT_ROLES,
}
ARGUMENT_SUPPORT_ROLES = {
    "conceptual",
    "interpretive",
    "normative",
    "methodological",
    "practitioner_guidance",
}

_GENERIC_FAMILY_RELATION_TERMS = {
    "analysis",
    "approach",
    "affect",
    "affecting",
    "change",
    "condition",
    "context",
    "design",
    "determine",
    "determining",
    "determinant",
    "dynamic",
    "evidence",
    "empirical",
    "factor",
    "finding",
    "framework",
    "governance",
    "institution",
    "institutional",
    "identify",
    "identifying",
    "influence",
    "influencing",
    "issue",
    "literature",
    "mechanism",
    "outcome",
    "policy",
    "practice",
    "process",
    "public",
    "relationship",
    "result",
    "role",
    "qualitative",
    "quantitative",
    "strategy",
    "study",
    "studies",
    "system",
}
_BROAD_FIELD_TERMS = {
    "civil",
    "conflict",
    "conflicts",
    "crises",
    "crisis",
    "dispute",
    "disputes",
    "international",
    "mediation",
    "mediator",
    "peace",
    "politic",
    "war",
    "wars",
}

# This is deliberately domain-neutral.  Thematic mapping needs to recognize
# that a paper operationalizing an outcome as settlement or agreement can
# belong to a literature framed around success or effectiveness.  Exact
# proposition comparison remains separate and does not use these aliases.
_THEMATIC_OUTCOME_TERMS = {
    "agreement",
    "consequence",
    "effect",
    "effective",
    "effectiveness",
    "failure",
    "impact",
    "outcome",
    "performance",
    "resolution",
    "result",
    "settlement",
    "success",
    "successful",
}

# Proposal prose often contains process words that describe how the reasoner
# formed a packet rather than what the literature is about.  They must not
# become the only terms a source has to match for thematic admission.
_THEMATIC_LINKING_GENERIC_TERMS = {
    "dataset",
    "often",
    "systematic",
    "systematically",
    "use",
    "used",
    "using",
}

CAUSAL_LANGUAGE = re.compile(
    r"\b(?:caus(?:e|es|ed|al|ality|ation)|effect|leads? to|produces?|drives?|results? in|"
    r"improv(?:e|es|ed)|enhanc(?:e|es|ed)|increas(?:e|es|ed)|reduc(?:e|es|ed)|"
    r"undermin(?:e|es|ed)|hinder(?:s|ed)?|prevent(?:s|ed)?|"
    r"facilitat(?:e|es|ed)|helps?(?:\s+to)?|work(?:s|ed)? best|succeeds? when)\b",
    re.I,
)
ATTRIBUTED_RELATIONSHIP = re.compile(
    r"\b(?:assert(?:s|ed)?|claim(?:s|ed)?|argu(?:e|es|ed)|recommend(?:s|ed)?|"
    r"advocat(?:e|es|ed)|propos(?:e|es|ed)|guidance|reported?|describ(?:e|es|ed)|"
    r"find(?:s|ing)?|observ(?:e|es|ed|ation)|identif(?:y|ies|ied)|converg(?:e|es|ed|ing))\b",
    re.I,
)
ATTRIBUTED_ARGUMENT = re.compile(
    r"\b(?:assert(?:s|ed)?|claim(?:s|ed)?|argu(?:e|es|ed)|recommend(?:s|ed)?|"
    r"advocat(?:e|es|ed)|propos(?:e|es|ed)|guidance|converg(?:e|es|ed|ing)\s+"
    r"on\s+(?:the\s+)?proposition)\b",
    re.I,
)
CONSENSUS_LANGUAGE = re.compile(r"\bconsensus\b", re.I)
NEGATED_CONSENSUS_LANGUAGE = re.compile(
    r"\b(?:no|without|lacks?|lacking)\b[^.!?]{0,60}\bconsensus\b|"
    r"\b(?:does|do|did|is|are|was|were)\s+not\b[^.!?]{0,60}\bconsensus\b|"
    r"\bconsensus\b[^.!?]{0,30}\b(?:not|unestablished|absent)\b",
    re.I,
)
NONCAUSAL_RELATIONSHIP = re.compile(
    r"\b(?:associat(?:e|es|ed|ion)|correlat(?:e|es|ed|ion)|link(?:s|ed)?|"
    r"predict(?:s|ed|or)?|relationship|odds?|probabilit(?:y|ies)|marginal effect)\b",
    re.I,
)
CAUSAL_NEGATION = re.compile(
    r"\b(?:no|none|neither|not|cannot|can't|does not|do not|lack(?:s|ed|ing)?|absence|"
    r"unsupported|unsubstantiated|insufficient|without)\b"
    r"[^.!?;]{0,80}\b(?:caus(?:e|es|ed|al|ality|ation)|produc(?:e|es|ed)|"
    r"leads?\s+to|drives?|results?\s+in|improv(?:e|es|ed)|increas(?:e|es|ed)|"
    r"reduc(?:e|es|ed))\b|"
    r"\b(?:rather\s+than|not)\b[^.!?;]{0,40}\bcaus(?:e|es|ed|al|ality|ation)\b|"
    r"\b(?:no|not)\s+(?:statistically\s+)?significant\s+effect\b",
    re.I,
)
INTERNAL_PROJECTION_ID = re.compile(
    r"\b(?:anchor|proposition|assertion|finding|claim)-[a-z0-9][a-z0-9-]*\b",
    re.I,
)
MIN_CLUSTER_VERDICT_WORDS = 50

CLUSTER_SYNTHESIS_SECTIONS = (
    "evidence_threads",
    "source_contributions",
    "central_findings",
    "agreements",
    "positions",
    "contradictions",
    "boundary_conditions",
    "methodological_fault_lines",
    "related_clusters",
    "source_roles",
)


class LiteratureSynthesisPartialError(RuntimeError):
    """A resumable literature-synthesis budget or deadline stop."""


_PROVIDER_INPUT_DEPENDENCY_COMPONENTS = {
    "stage",
    "key",
    "method",
    "provider",
    "model",
    "source_set_id",
    "profile_dependencies",
    "context",
    "policy",
    "prompt_version",
}


def _has_unqualified_causal_language(value: Any) -> bool:
    text = str(value or "")
    if re.match(
        r"^\s*(?:The cited sources? report that\b|In the cited non-causal evidence,|This is a reported link, not proof of cause:)",
        text,
        flags=re.I,
    ):
        return False
    clauses = re.split(
        r"(?<=[.!?;])\s+|\n+|,\s+(?=(?:but|while|whereas|although|yet)\b)|"
        r"\b(?:but|while|whereas|although|yet)\b|"
        r"\band\s+(?=(?:caus(?:e|es|ed)|leads?\s+to|produces?|drives?|results?\s+in|"
        r"improv(?:e|es|ed)|enhanc(?:e|es|ed)|increas(?:e|es|ed)|reduc(?:e|es|ed)|"
        r"undermin(?:e|es|ed)|hinder(?:s|ed)?|prevent(?:s|ed)?|"
        r"facilitat(?:e|es|ed)|helps?(?:\s+to)?|work(?:s|ed)?\s+best|"
        r"succeeds?\s+when|makes?)\b)",
        text,
        flags=re.I,
    )
    for sentence in clauses:
        causal_comparison = re.search(
            r"\b(?:more|less)\s+effective\b|"
            r"\bmakes?\b[^.!?]{0,80}\b(?:(?:more|less)\s+(?:durable|sustainable|successful|effective)|better|worse)\b",
            sentence,
            flags=re.I,
        )
        if not CAUSAL_LANGUAGE.search(sentence) and not causal_comparison:
            continue
        # Reporting or observing a relationship does not make causal wording
        # safe: those verbs commonly introduce empirical findings whose support
        # envelope may still be descriptive or associational. Explicitly
        # attributed arguments and guidance are different because the sentence
        # reports a position rather than treating it as an estimated effect.
        if (
            ATTRIBUTED_ARGUMENT.search(sentence)
            or NONCAUSAL_RELATIONSHIP.search(sentence)
            or CAUSAL_NEGATION.search(sentence)
        ):
            continue
        return True
    return False


def _causal_support_boundary(values: Iterable[Any]) -> str:
    """Select a claim-specific inference limit from support-envelope restrictions."""

    restrictions = [
        re.sub(r"\s+", " ", str(value or "")).strip().rstrip(".")
        for value in values
        if str(value or "").strip()
    ]
    for restriction in restrictions:
        if re.search(
            r"\b(?:caus|selection|observational|counterfactual|exclusion restriction|"
            r"confound|generaliz|single[- ]case|illustrative|not test)\w*\b",
            restriction,
            flags=re.I,
        ):
            return restriction[:1].upper() + restriction[1:]
    return (
        "This evidence does not by itself establish that the first factor caused "
        "the reported outcome"
    )


def _anchor_supports_causal_claim(anchor: Mapping[str, Any]) -> bool:
    """Require both a causal role and an envelope that does not retract causality."""

    envelope = _as_mapping(anchor.get("support_envelope"))
    if str(envelope.get("empirical_role") or "none") not in CAUSAL_SUPPORT_ROLES:
        return False
    restrictions = " ".join(
        str(value) for value in envelope.get("restrictions", []) or [] if str(value)
    )
    return not bool(
        re.search(
            r"\b(?:does not|cannot|can not|not able to|fails? to)\b[^.!?;]{0,80}\bcaus|"
            r"\bcausal (?:link|effect|relationship|interpretation)\b[^.!?;]{0,60}"
            r"\b(?:unproven|unsupported|uncertain|contestable)|"
            r"\bexclusion restriction\b[^.!?;]{0,60}\b(?:contestable|uncertain|weak)|"
            r"\bobservational\b[^.!?;]{0,60}\b(?:cannot|does not|without)\b",
            restrictions,
            flags=re.I,
        )
    )


def _narrow_noncausal_organizational_language(
    value: Any, *, boundary: str = ""
) -> str:
    """Attribute non-causal evidence without rewriting sentence grammar.

    Earlier versions replaced individual causal verbs with association phrases.
    That could turn perfectly readable provider prose into fragments such as
    ``tenfold is associated with higher in``.  Whole-sentence attribution keeps
    the source's wording visible while making the evidentiary boundary explicit.
    """

    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text or not _has_unqualified_causal_language(text):
        return text
    attributed = text[:1].lower() + text[1:]
    return (
        f"The cited sources report that {attributed} "
        "This evidence does not by itself establish causation."
    )


def _narrow_unadjudicated_gap_language(value: Any) -> str:
    """Describe missing collection evidence without promoting a gap by prose alone."""

    text = str(value or "")
    replacements = (
        (
            r"\b(?:critical|important|clear|major)\s+(?:research\s+|evidence\s+|knowledge\s+|literature\s+)?gap\b",
            "important limitation in this collection",
        ),
        (
            r"\b(?:research|evidence|knowledge|literature)\s+gap\b",
            "unresolved question in this collection",
        ),
        (
            r"\bgap\s+in\s+(?:the\s+)?(?:research|evidence|knowledge|literature)\b",
            "limited evidence in this collection",
        ),
    )
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, flags=re.I)
    text = re.sub(r"\ba important\b", "an important", text, flags=re.I)
    return text


def _replace_cluster_item_statement(item: dict[str, Any], statement: str) -> None:
    """Replace the first human assertion field while preserving section shape."""

    for key in (
        "assertion",
        "finding",
        "position",
        "agreement",
        "contradiction",
        "text",
        "summary",
    ):
        if str(item.get(key) or "").strip():
            item[key] = statement
            return
    item["text"] = statement


def _asserts_consensus(value: Any) -> bool:
    """Treat every affirmative consensus formulation as a strict three-base claim."""

    text = " ".join(_flatten_values(value)).strip()
    return bool(
        CONSENSUS_LANGUAGE.search(text) and not NEGATED_CONSENSUS_LANGUAGE.search(text)
    )


def _human_projection_text(value: Any) -> str:
    """Remove machine-local IDs from prose while preserving readable citations."""

    if isinstance(value, Mapping) or (
        isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))
    ):
        text = "; ".join(_flatten_values(value))
    else:
        text = str(value or "")
    stripped = text.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        try:
            parsed = ast.literal_eval(stripped)
        except (SyntaxError, ValueError):
            parsed = None
        if isinstance(parsed, (list, tuple)) and all(
            isinstance(item, str) for item in parsed
        ):
            text = "; ".join(item.strip() for item in parsed if item.strip())
    text = re.sub(
        r"\b(?:proposition|finding|claim)-[a-z0-9][a-z0-9-]*\b\s*;?\s*",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(
        r",?\s*\b(?:anchor|assertion)-[a-z0-9][a-z0-9-]*\b", "", text, flags=re.I
    )
    # Provider prose occasionally repeats compact author-year keys such as
    # Kane2022.  Preserve the attribution in a readable form; deleting the key
    # leaves broken sentences such as "and both report".
    def citation_key_replacement(match: re.Match[str]) -> str:
        name, year = match.groups()
        if re.search(r"[A-Z]{2,}", name):
            name = name.upper()
        else:
            name = name[:1].upper() + name[1:]
        return f"{name} ({year})"

    text = re.sub(
        r"\b([A-Za-zÀ-ÖØ-öø-ÿ-]{3,})(\d{4}[a-z]?)\b",
        citation_key_replacement,
        text,
    )
    text = re.sub(
        r"\bdowngrade\s+(?:the\s+)?conditional relationship\s+from\s+['\"]conditional_relationship['\"]\s+to\s+['\"]unsupported['\"]",
        "weaken confidence in the reported relationship",
        text,
        flags=re.I,
    )
    enum_labels = {
        "conditional_relationship": "context-dependent finding",
        "complementary_positions": "complementary findings",
        "parallel_literatures": "related but non-comparable literatures",
        "within_program_consistency": "compatible findings from one research program",
        "aligned_institutional_guidance": "aligned practitioner guidance",
    }
    for enum_value, label in enum_labels.items():
        text = re.sub(rf"\b{re.escape(enum_value)}\b", label, text, flags=re.I)
    # Keep attribution while removing the nested boilerplate that language
    # models sometimes repeat around an already attributed source finding.
    text = re.sub(
        r"\b((?:[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’-]+(?:\s+and\s+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’-]+)?\s+\(\d{4}[a-z]?\))\s+reports? that)\s+the cited sources report that\s+",
        r"\1 ",
        text,
    )
    text = re.sub(
        r"\bThe cited sources report that\s+(both|all|each)\b",
        lambda match: match.group(1).capitalize(),
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bThe cited sources report that\s+([A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’-]+\s+\(\d{4}[a-z]?\))\s+(?:finds?|reports?|shows?)\s+that\s+",
        r"\1 reports that ",
        text,
    )
    text = re.sub(
        r"\bThe cited sources report that\b", "The studies report that", text
    )
    text = re.sub(
        r"\bThe studies report that\s+the studies report that\s+",
        "The studies report that ",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bThe studies report that\s+([A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’-]+\s+\(\d{4}[a-z]?\))\s+(?:finds?|reports?|shows?)\s+that\s+",
        r"\1 reports that ",
        text,
    )
    text = re.sub(
        r"(^|(?<=[.!?])\s+)The studies report that\s+([a-z])",
        lambda match: match.group(1) + match.group(2).upper(),
        text,
    )
    text = re.sub(r"\bthe studies report that\b", "the evidence indicates that", text)
    text = re.sub(r"\bAll sources converge on\b", "The cited sources repeatedly emphasize", text, flags=re.I)
    text = re.sub(r"\buN\b", "UN", text)
    text = re.sub(
        r"\b([a-z][a-zà-öø-ÿ'’-]+)\s+\((19\d{2}|20\d{2})\)",
        lambda match: f"{match.group(1).capitalize()} ({match.group(2)})",
        text,
    )
    text = re.sub(r"\(\s*[;,]\s*", "(", text)
    text = re.sub(r"\s*[;,]\s*\)", ")", text)
    text = re.sub(r"\(\s*\)", "", text)
    # Repair prose produced by older local causal-narrowing checkpoints. These
    # are projection-only grammar repairs; they add no evidentiary claim.
    text = re.sub(
        r"\b(aims?|seeks?)\s+to\s+is associated with lower\b",
        r"\1 to reduce",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\b(can|could|may|might)\s+is associated with\b",
        r"may be associated with",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\b(?:a\s+)?big is associated with higher in\b",
        "a large increase in",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bmain is associated with of\b",
        "main reported cause of",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bfor (?:a\s+)?large is associated with higher\b",
        "for a large increase",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\b(mediators?) is associated with\b",
        r"\1 are associated with",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\b([Bb]oth types) is associated with\b",
        r"\1 are associated with",
        text,
    )
    text = re.sub(
        r"\bassociated with higher the (probability|likelihood|odds|rate)\b",
        r"associated with a higher \1",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bis associated with lower it\b",
        "is associated with a lower probability of success",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\balmost guarantees success\b",
        "corresponds to a very high model-predicted probability of success",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\balmost guarantees failure\b",
        "corresponds to a very high model-predicted probability of failure",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bMediation is almost certain to succeed\b",
        "The fitted model predicts a very high probability of mediation success",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bit almost certainly fails\b",
        "the fitted model predicts a very high probability of failure",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bproven techniques\b",
        "practitioner-recommended techniques",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bAfrican mediators are far more effective than non-African ones at achieving peace agreements\b",
        (
            "In this African civil-war sample, African mediation is associated with a much higher "
            "probability of a negotiated settlement than non-African mediation"
        ),
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bmechanisms that is associated with higher women's opportunities\b",
        "institutional arrangements that may expand women's opportunities",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bPeacebuilding only works when all sides agree to a cease-?fire and a political deal\b",
        (
            "In Hampson's case comparisons, successful operations shared a ceasefire and "
            "political settlement accepted by all sides"
        ),
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bMediation works best when mediators are seen as fair, include all relevant groups, "
        r"are well-prepared, and follow international norms\b",
        (
            "The guidance recommends impartiality, inclusion, preparation, and adherence "
            "to international norms; it does not itself test whether these practices cause better outcomes"
        ),
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bMediation works best when mediators are well-trained and teams are balanced\. "
        r"Too much outside pressure can make agreements fail, and mediators must avoid roles that compromise their neutrality\b",
        (
            "The cited practice literature recommends well-trained mediators and balanced teams. "
            "Its case material also warns that outside pressure may undermine agreements and that mediators should avoid roles that compromise neutrality"
        ),
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bTogether, they suggest that legitimacy compensates for capacity deficits\b",
        (
            "Read together, the studies raise—but do not test—the possibility that legitimacy "
            "may offset some capacity constraints"
        ),
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bAfrican mediators achieve results through legitimacy even when institutional support is weak, "
        r"implying that normative alignment matters more than material resources\b",
        (
            "Duursma associates African mediation with settlement outcomes, while Vines documents "
            "institutional constraints; the collection does not directly compare legitimacy with material capacity"
        ),
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bDuursma shows that African mediators are effective despite having fewer material resources, "
        r"attributing this to legitimacy from the 'African solutions' norm\b",
        (
            "Duursma reports higher settlement probabilities for African mediation despite fewer material "
            "resources and interprets the pattern as consistent with legitimacy from the 'African solutions' norm"
        ),
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bDirective mediation strategy increases success probability by\b",
        "Directive mediation strategy is associated with a success-probability difference of",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bUsing directive mediation raises the chance of success from about 38% to about 45%",
        (
            "In this observational model, directive mediation corresponds to about 45% predicted success "
            "versus 38% otherwise; the estimate is borderline at p=0.068"
        ),
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\b(\d+|[A-Z][a-z]+) sources converge on the finding that\b",
        r"\1 sources address a shared pattern:",
        text,
    )
    text = re.sub(
        r"\bAll sources identify\b",
        "The mapped sources repeatedly identify",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\b([0-9.]+-?fold|tenfold) is associated with higher in\b",
        r"\1 increase in",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\baddressing root is associated with\b",
        "addressing root causes",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bare associated with a ([0-9.]+ percentage point) is associated with higher in\b",
        r"is associated with a \1 increase in",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bis associated with a ([0-9.]+)% is associated with higher in\b",
        r"is associated with a \1 percentage-point increase in",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bgovernment-biased mediators significantly is associated with\b",
        "government-biased mediators are significantly associated with",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bDuursma finds African mediation raises the probability of a negotiated settlement by\b",
        "Duursma finds African mediation is associated with a negotiated-settlement probability that is higher by",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bHampson \(1997\) argues that peacebuilding operations succeed only when linked to comprehensive cease-?fire and political settlements agreed by all parties\b",
        (
            "In Hampson's (1997) cases, successful peacebuilding operations shared comprehensive ceasefire "
            "and political settlements accepted by all parties"
        ),
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bMultiple sources demonstrate that prevention is cost-effective\b",
        (
            "Several policy reports argue that prevention can be cost-effective, drawing on scenario "
            "calculations and case estimates"
        ),
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bPrevention saves money compared to responding to conflicts after they escalate\b",
        (
            "The scenario calculations estimate that prevention can cost less than responding "
            "after conflicts escalate, under their stated assumptions"
        ),
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bUnder middle-of-the-road assumptions, prevention saves about \$?33 billion globally each year\b",
        "Under the middle scenario, the model estimates about US$33 billion in annual global net savings",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bEven under the (?:worst|most conservative) assumptions(?: about prevention effectiveness and cost)?, "
        r"prevention (?:still saves|yields net savings of) about \$?5 billion each year(?: compared to not doing prevention)?\b",
        "In the model's pessimistic scenario, estimated annual net savings are about US$5 billion",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bHaving a government-biased mediator is associated with a 14% higher chance of reaching a peace agreement\b",
        (
            "In this observational model, government-biased mediation corresponds to a 14-percentage-point "
            "higher predicted probability of a negotiated settlement"
        ),
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bEach additional prior mediation experience by the same mediator are associated with\b",
        "Each additional prior mediation experience by the same mediator is associated with",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bEach additional previous attempt by the same mediator are associated with lower success probability by ([0-9.]+) percentage points\b",
        r"Each additional previous attempt by the same mediator is associated with a \1 percentage-point lower success probability",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bMediation works better when both parties want it, on neutral ground, over simple issues, and early in the conflict\b",
        (
            "In these observational data, success is more common when both parties initiate mediation, "
            "the venue is neutral, issues are less complex, and intervention occurs earlier"
        ),
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bEach additional prior attempt by the same mediator reduces success probability by about "
        r"([0-9.]+) percentage points\b",
        (
            r"Each additional prior attempt by the same mediator is associated with about a "
            r"\1-percentage-point lower predicted probability of success"
        ),
        text,
        flags=re.I,
    )
    text = re.sub(r"\.\s*;", ";", text)
    text = re.sub(r"(?<!\d)\.\.(?!\d)", ".", text)
    text = re.sub(r"\bindependent estimates\b", "estimates", text, flags=re.I)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _human_technical_result(value: Any) -> str:
    """Keep inspectable technical detail and suppress extraction debris."""

    text = _human_projection_text(value)
    text = re.sub(
        r"^\s*(?:not[_ ]reported|none|n/?a)\s*[;,:-]*\s*",
        "",
        text,
        flags=re.I,
    ).strip()
    if not text:
        return ""
    if re.match(r"^(?:than|compared\s+to|versus)\b", text, flags=re.I):
        return ""
    if not (
        re.search(r"\d", text)
        or re.search(
            r"\b(?:coefficient|estimate|odds ratio|hazard ratio|marginal effect|"
            r"confidence interval|standard error|p\s*[<=>]|regression|model|sample|"
            r"probability|percentage|percent|rate|mean|median|variance|robustness)\b",
            text,
            flags=re.I,
        )
    ):
        return ""
    return text


def _human_locator_text(value: Any) -> str:
    """Normalize compact page markers for researcher-facing citations."""

    text = _human_projection_text(value)
    if text.casefold().strip(" .:-") in {
        "abstract",
        "analysis",
        "data",
        "findings",
        "limitation",
        "limitations",
        "method",
        "methods",
        "results",
        "statistical context",
        "thesis",
    }:
        return ""
    return text


_MALFORMED_HUMAN_PROSE = (
    re.compile(r"\bsignificantly is associated\b", re.I),
    re.compile(
        r"\b(?:is|are) associated with (?:higher|lower) (?:in|of|it|from|the)\b", re.I
    ),
    re.compile(r"\b(?:tenfold|\d+(?:\.\d+)?-?fold) is associated\b", re.I),
    re.compile(r"\baddressing root is associated\b", re.I),
    re.compile(r"\bmay be associated with a [a-z-]+ to (?:fail|succeed)\b", re.I),
    re.compile(r"\b(?:and|or|versus|compared with|compared to)\s*$", re.I),
    re.compile(r"\.\.\.\s*$"),
)


def _human_prose_errors(value: Any) -> list[str]:
    """Return projection-blocking grammar defects after safe human cleanup."""

    text = _human_projection_text(value)
    return [
        f"malformed_human_prose:{index}"
        for index, pattern in enumerate(_MALFORMED_HUMAN_PROSE, start=1)
        if pattern.search(text)
    ]


def _counted_noun(count: int, singular: str, plural: str | None = None) -> str:
    """Render count-aware nouns in deterministic researcher prose."""

    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


class _CheckpointedReasonerCalls:
    """Budgeted, resumable provider calls for collection-level reasoning."""

    def __init__(
        self,
        workspace: Path,
        run_id: str,
        reasoner: Any,
        request: Any,
        *,
        stage_callback: Callable[..., Any] | None = None,
    ) -> None:
        self.workspace = workspace
        self.run_id = run_id
        self.reasoner = reasoner
        self.request = request
        request_values = _as_mapping(request) if request is not None else {}
        policy = request_values.get("literature_policy")
        self.max_calls = int(_policy_value(policy, "max_synthesis_calls", 24))
        self.deadline_seconds = float(
            _policy_value(policy, "literature_deadline_seconds", 1_800.0)
        )
        self.root = (
            workspace / "11_state" / "runs" / run_id / "literature" / "synthesis"
        )
        self.stage_callback = stage_callback
        self.started = time.monotonic()
        self.provider_calls = 0
        self.checkpoint_hits = 0
        self.failures = 0
        self.synthesized_clusters = 0
        self._synthesized_cluster_ids: set[str] = set()

    def __call__(
        self,
        stage: str,
        key: str,
        method_name: str,
        profiles: Sequence[Any],
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        method = getattr(self.reasoner, method_name, None)
        if not callable(method):
            return {}
        if time.monotonic() - self.started >= self.deadline_seconds:
            raise LiteratureSynthesisPartialError("literature_stage_deadline_reached")

        enriched_context = dict(context)
        if stage == "cluster_synthesis":
            enriched_context["atomic_notes"] = self._atomic_notes(profiles)
        dependency = {
            "stage": stage,
            "key": key,
            "method": method_name,
            "provider": str(getattr(self.reasoner, "name", "")),
            "model": str(getattr(self.reasoner, "model", "")),
            "source_set_id": str(_as_mapping(self.request).get("source_set_id") or ""),
            "profile_dependencies": [
                {
                    "source_id": str(_as_mapping(profile).get("source_id") or ""),
                    "note_hash": str(_as_mapping(profile).get("note_hash") or ""),
                    "dependency_hash": str(
                        _as_mapping(profile).get("dependency_hash") or ""
                    ),
                    "profile_schema_version": str(
                        _as_mapping(profile).get("profile_schema_version") or ""
                    ),
                    # Note and anchor hashes alone do not capture synthesis
                    # eligibility, support-envelope downgrades, typed
                    # quantitative records, or other profile-level repairs.
                    # Hash the stable semantic profile projection so a repaired
                    # profile cannot reuse a proposal produced from its older
                    # excluded or weaker state.
                    "profile_content_hash": _stable_hash(
                        _checkpoint_dependency_context(_as_mapping(profile))
                    ),
                    "anchor_revisions": sorted(
                        str(_as_mapping(anchor).get("revision_hash") or "")
                        for anchor in _as_mapping(profile).get("evidence_anchors", [])
                        or []
                        if _as_mapping(anchor).get("revision_hash")
                    ),
                }
                for profile in profiles
            ],
            "context": _checkpoint_dependency_context(
                enriched_context,
                sort_sequences=stage == "gap_adjudication",
            ),
            "policy": _as_mapping(_as_mapping(self.request).get("literature_policy"))
            if self.request is not None
            else {},
            "prompt_version": _synthesis_stage_prompt_version(stage),
            "algorithm_version": LITERATURE_ALGORITHM_VERSION,
            "anchor_algorithm_version": ANCHOR_ALGORITHM_VERSION,
            "support_envelope_version": SUPPORT_ENVELOPE_VERSION,
            "proposition_algorithm_version": PROPOSITION_ALGORITHM_VERSION,
            "proposition_matrix_version": PROPOSITION_MATRIX_VERSION,
            "gap_rule_version": GAP_RULE_VERSION,
            "family_relation_version": FAMILY_RELATION_VERSION,
            "family_admission_version": FAMILY_ADMISSION_VERSION,
            "strict_adjudication_version": STRICT_ADJUDICATION_VERSION,
            "study_lineage_version": STUDY_LINEAGE_VERSION,
            "independence_algorithm_version": INDEPENDENCE_ALGORITHM_VERSION,
            "quantitative_validation_version": QUANTITATIVE_VALIDATION_VERSION,
            "locator_audit_version": LOCATOR_AUDIT_VERSION,
        }
        fingerprint = _stable_hash(dependency)
        dependency_component_hashes = {
            str(component): _stable_hash(value)
            for component, value in dependency.items()
        }
        dependency_context_hashes = {
            str(component): _stable_hash(value)
            for component, value in _as_mapping(dependency.get("context")).items()
        }
        dependency_context_item_hashes: dict[str, dict[str, str]] = {}
        for component, values in _as_mapping(dependency.get("context")).items():
            if not isinstance(values, Sequence) or isinstance(
                values, (str, bytes, bytearray)
            ):
                continue
            item_hashes: dict[str, str] = {}
            for index, value in enumerate(values):
                if not isinstance(value, Mapping):
                    continue
                item = value
                identifier = str(
                    item.get("gap_id")
                    or item.get("cluster_id")
                    or item.get("source_id")
                    or index
                )
                item_hashes[identifier] = _stable_hash(value)
            if item_hashes:
                dependency_context_item_hashes[str(component)] = item_hashes
        # A checkpoint is reusable only when the complete provenance contract
        # still matches. In particular, v0.5 dimension matrices and document-
        # level cluster prompts must never be upgraded into proposition-backed
        # outputs without a fresh synthesis call.
        compatible_fingerprints = {fingerprint}
        path = (
            self.root
            / safe_filename(stage)
            / f"{safe_filename(key, fallback='packet')}.yml"
        )
        failure_path = (
            self.root
            / "failures"
            / safe_filename(stage)
            / f"{safe_filename(key, fallback='packet')}.yml"
        )
        history_root = (
            self.root
            / "history"
            / safe_filename(stage)
            / safe_filename(key, fallback="packet")
        )
        existing = read_yaml(path, {}) or {}
        matching_checkpoint: Mapping[str, Any] | None = None
        if (
            isinstance(existing, Mapping)
            and existing.get("fingerprint") in compatible_fingerprints
        ):
            matching_checkpoint = existing
        if matching_checkpoint is None:
            for compatible_fingerprint in sorted(compatible_fingerprints):
                historical = (
                    read_yaml(history_root / f"{compatible_fingerprint}.yml", {}) or {}
                )
                if (
                    isinstance(historical, Mapping)
                    and historical.get("fingerprint") == compatible_fingerprint
                    and isinstance(historical.get("response"), Mapping)
                ):
                    matching_checkpoint = historical
                    break
        if matching_checkpoint is not None and isinstance(
            matching_checkpoint.get("response"), Mapping
        ):
            if existing.get("fingerprint") != fingerprint or not isinstance(
                existing.get("response"), Mapping
            ):
                self._archive_successful_checkpoint(existing, history_root)
                write_yaml(
                    path,
                    {
                        **dict(matching_checkpoint),
                        "fingerprint": fingerprint,
                        "upgraded_from_fingerprint": str(
                            matching_checkpoint.get("fingerprint") or ""
                        ),
                        "updated_at": now_iso(),
                    },
                )
            if failure_path.exists():
                failure_path.unlink()
            self.checkpoint_hits += 1
            if stage == "cluster_synthesis":
                self._mark_synthesized_cluster(key)
            self._progress(stage, path, active=False)
            return dict(matching_checkpoint["response"])
        # A deterministic admission/validation repair must not repeat a paid
        # provider call when the provider-visible prompt and inputs are
        # unchanged. Revalidate the preserved response under the current local
        # contract and give it the new complete fingerprint.
        provider_input_candidates = [existing]
        if history_root.is_dir():
            provider_input_candidates.extend(
                read_yaml(candidate, {}) or {}
                for candidate in sorted(history_root.glob("*.yml"))
            )
        for prior in provider_input_candidates:
            if not _same_provider_inputs(
                prior,
                dependency_component_hashes,
                stage=stage,
                current_context_hashes=dependency_context_hashes,
            ):
                continue
            prior_response = prior.get("response")
            if not isinstance(prior_response, Mapping):
                continue
            recovered_response = _revalidate_raw_synthesis_response(
                stage, prior_response
            )
            if recovered_response is None:
                continue
            self._archive_successful_checkpoint(existing, history_root)
            write_yaml(
                path,
                {
                    "checkpoint_schema_version": "1",
                    "fingerprint": fingerprint,
                    "stage": stage,
                    "key": key,
                    "status": "completed",
                    "provider": str(getattr(self.reasoner, "name", "")),
                    "model": str(getattr(self.reasoner, "model", "")),
                    "dependency_component_hashes": dependency_component_hashes,
                    "dependency_context_hashes": dependency_context_hashes,
                    "dependency_context_item_hashes": dependency_context_item_hashes,
                    "response": recovered_response,
                    "revalidated_from_provider_input_fingerprint": str(
                        prior.get("fingerprint") or ""
                    ),
                    "updated_at": now_iso(),
                },
            )
            if failure_path.exists():
                failure_path.unlink()
            self.checkpoint_hits += 1
            if stage == "cluster_synthesis":
                self._mark_synthesized_cluster(key)
            self._progress(stage, path, active=False)
            return recovered_response
        failed_checkpoints = [
            checkpoint
            for checkpoint in (
                existing,
                read_yaml(failure_path, {}) or {},
            )
            if isinstance(checkpoint, Mapping)
            and checkpoint.get("fingerprint") == fingerprint
            and isinstance(checkpoint.get("raw_response"), Mapping)
        ]
        for failed_checkpoint in failed_checkpoints:
            recovered_response = _revalidate_raw_synthesis_response(
                stage,
                failed_checkpoint["raw_response"],
            )
            if recovered_response is None:
                continue
            write_yaml(
                path,
                {
                    "checkpoint_schema_version": "1",
                    "fingerprint": fingerprint,
                    "stage": stage,
                    "key": key,
                    "status": "completed",
                    "provider": str(getattr(self.reasoner, "name", "")),
                    "model": str(getattr(self.reasoner, "model", "")),
                    "dependency_component_hashes": dependency_component_hashes,
                    "dependency_context_hashes": dependency_context_hashes,
                    "dependency_context_item_hashes": dependency_context_item_hashes,
                    "response": recovered_response,
                    "recovered_from_failed_raw_response": True,
                    "updated_at": now_iso(),
                },
            )
            if failure_path.exists():
                failure_path.unlink()
            self.checkpoint_hits += 1
            if stage == "cluster_synthesis":
                self._mark_synthesized_cluster(key)
            self._progress(stage, path, active=False)
            return recovered_response
        if self.provider_calls >= self.max_calls:
            raise LiteratureSynthesisPartialError(
                "literature_synthesis_call_budget_reached"
            )

        self._archive_successful_checkpoint(existing, history_root)
        self.provider_calls += 1
        self._progress(stage, path, active=True)
        try:
            response = method(profiles, self.request, context=enriched_context)
            if is_dataclass(response):
                response = asdict(response)
            if not isinstance(response, Mapping):
                raise ValueError(f"{method_name} must return a mapping")
            payload = {
                "checkpoint_schema_version": "1",
                "fingerprint": fingerprint,
                "stage": stage,
                "key": key,
                "status": "completed",
                "provider": str(getattr(self.reasoner, "name", "")),
                "model": str(getattr(self.reasoner, "model", "")),
                "dependency_component_hashes": dependency_component_hashes,
                "dependency_context_hashes": dependency_context_hashes,
                "dependency_context_item_hashes": dependency_context_item_hashes,
                "response": dict(response),
                "updated_at": now_iso(),
            }
            write_yaml(path, payload)
            if failure_path.exists():
                failure_path.unlink()
            if stage == "cluster_synthesis":
                self._mark_synthesized_cluster(key)
            return dict(response)
        except Exception as exc:
            self.failures += 1
            raw_response = getattr(self.reasoner, "last_literature_response", None)
            failure_payload = {
                "checkpoint_schema_version": "1",
                "fingerprint": fingerprint,
                "stage": stage,
                "key": key,
                "status": "failed",
                "provider": str(getattr(self.reasoner, "name", "")),
                "model": str(getattr(self.reasoner, "model", "")),
                "dependency_component_hashes": dependency_component_hashes,
                "dependency_context_hashes": dependency_context_hashes,
                "dependency_context_item_hashes": dependency_context_item_hashes,
                "error": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
                "updated_at": now_iso(),
            }
            if isinstance(raw_response, Mapping):
                failure_payload["raw_response"] = dict(raw_response)
            target = (
                failure_path if isinstance(existing.get("response"), Mapping) else path
            )
            write_yaml(target, failure_payload)
            raise
        finally:
            self._progress(stage, path, active=False)

    @staticmethod
    def _archive_successful_checkpoint(existing: Any, history_root: Path) -> None:
        if not isinstance(existing, Mapping) or not isinstance(
            existing.get("response"), Mapping
        ):
            return
        existing_fingerprint = str(existing.get("fingerprint") or "")
        if not existing_fingerprint:
            return
        history_path = history_root / f"{existing_fingerprint}.yml"
        if not history_path.exists():
            write_yaml(history_path, dict(existing))

    def completed_responses(self, stage: str, key: str) -> list[Mapping[str, Any]]:
        """Return this run's successful current and historical stage responses.

        Coverage-repair inputs can legitimately change after a local admission
        fix: sources admitted by the repaired validator are no longer included
        in the next repair packet.  The earlier paid response is still part of
        the frozen run and must be revalidated alongside the new response,
        rather than silently disappearing from the resumed collection map.
        """

        current_path = (
            self.root
            / safe_filename(stage)
            / f"{safe_filename(key, fallback='packet')}.yml"
        )
        history_root = (
            self.root
            / "history"
            / safe_filename(stage)
            / safe_filename(key, fallback="packet")
        )
        candidates = [
            *(sorted(history_root.glob("*.yml")) if history_root.is_dir() else []),
            current_path,
        ]
        current_checkpoint = read_yaml(current_path, {}) or {}
        current_component_hashes = (
            dict(current_checkpoint.get("dependency_component_hashes", {}) or {})
            if isinstance(current_checkpoint, Mapping)
            else {}
        )
        responses: list[Mapping[str, Any]] = []
        seen: set[str] = set()
        expected_provider = str(getattr(self.reasoner, "name", ""))
        expected_model = str(getattr(self.reasoner, "model", ""))
        for path in candidates:
            checkpoint = read_yaml(path, {}) or {}
            if (
                not isinstance(checkpoint, Mapping)
                or checkpoint.get("status") != "completed"
                or not isinstance(checkpoint.get("response"), Mapping)
                or str(checkpoint.get("provider") or "") != expected_provider
                or str(checkpoint.get("model") or "") != expected_model
            ):
                continue
            candidate_component_hashes = dict(
                checkpoint.get("dependency_component_hashes", {}) or {}
            )
            if current_component_hashes and any(
                candidate_component_hashes.get(component) != expected_hash
                for component, expected_hash in current_component_hashes.items()
                if component != "context"
            ):
                # Historical responses remain useful when only repair context
                # changed after local admission fixes. They must never cross a
                # source-set, profile, provider, model, policy, prompt, or
                # algorithm boundary.
                continue
            response = dict(checkpoint["response"])
            response_hash = _stable_hash(response)
            if response_hash in seen:
                continue
            seen.add(response_hash)
            responses.append(response)
        return responses

    def _mark_synthesized_cluster(self, key: str) -> None:
        cluster_id = str(key).split("--repair", 1)[0]
        self._synthesized_cluster_ids.add(cluster_id)
        self.synthesized_clusters = len(self._synthesized_cluster_ids)

    def _atomic_notes(self, profiles: Sequence[Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        workspace = self.workspace.resolve()
        for profile in profiles:
            raw = _as_mapping(profile)
            context = (
                raw.get("context") if isinstance(raw.get("context"), Mapping) else {}
            )
            note_path = str(raw.get("note_path") or context.get("note_path") or "")
            if not note_path:
                continue
            path = (self.workspace / note_path).resolve()
            if path != workspace and workspace not in path.parents:
                continue
            if not path.is_file():
                continue
            rows.append(
                {
                    "source_id": str(raw.get("source_id") or ""),
                    "note_id": str(raw.get("note_id") or ""),
                    "note_path": note_path,
                    "markdown": path.read_text(encoding="utf-8"),
                }
            )
        return rows

    def _progress(self, stage: str, path: Path, *, active: bool) -> None:
        if self.stage_callback is None:
            return
        values = {
            "active_synthesis_packet": str(path) if active else "",
            "synthesis_call_count": self.provider_calls,
            "synthesis_checkpoint_hit_count": self.checkpoint_hits,
            "synthesized_cluster_count": self.synthesized_clusters,
            "synthesis_failure_count": self.failures,
        }
        if stage == "cluster_synthesis":
            values["active_cluster"] = path.stem if active else ""
        elif stage == "gap_adjudication":
            values["active_gap_packet"] = path.stem if active else ""
        _notify_stage(self.stage_callback, stage, **values)


def _notify_stage(
    callback: Callable[..., Any] | None, stage: str, **values: Any
) -> None:
    """Keep the legacy one-argument stage callback compatible with live progress callbacks."""

    if callback is None:
        return
    try:
        parameters = inspect.signature(callback).parameters.values()
    except (TypeError, ValueError):
        callback(stage, **values)
        return
    accepts_values = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters
    )
    if accepts_values:
        callback(stage, **values)
    else:
        callback(stage)


def _synthesis_stage_prompt_version(stage: str) -> str:
    return {
        "cluster_proposal": CLUSTER_PROPOSAL_PROMPT_VERSION,
        "cluster_synthesis": CLUSTER_SYNTHESIS_PROMPT_VERSION,
        "gap_adjudication": GAP_REASONING_PROMPT_VERSION,
    }.get(stage, LITERATURE_ALGORITHM_VERSION)


def _revalidate_raw_synthesis_response(
    stage: str,
    raw_response: Mapping[str, Any],
) -> dict[str, Any] | None:
    kind = {
        "cluster_proposal": "cluster_proposal",
        "cluster_synthesis": "cluster_synthesis",
        "gap_adjudication": "gap_adjudication",
    }.get(stage)
    if kind is None:
        return None
    # Keep provider transport independent from the collection mapper while
    # allowing a paid response to survive a local schema-adapter repair.
    from .readers import _validate_literature_response

    try:
        return _validate_literature_response(raw_response, kind=kind)
    except (TypeError, ValueError, RuntimeError):
        return None


def _same_provider_inputs(
    checkpoint: Any,
    current_component_hashes: Mapping[str, str],
    *,
    stage: str = "",
    current_context_hashes: Mapping[str, str] | None = None,
) -> bool:
    if not isinstance(checkpoint, Mapping):
        return False
    prior_hashes = checkpoint.get("dependency_component_hashes")
    if not isinstance(prior_hashes, Mapping):
        return False
    exact_match = all(
        str(prior_hashes.get(component) or "")
        == str(current_component_hashes.get(component) or "")
        for component in _PROVIDER_INPUT_DEPENDENCY_COMPONENTS
    )
    if exact_match:
        return True
    # Provider adapters use compact stage-specific packets. A successful call
    # remains reusable when every provider-visible input is unchanged even if a
    # richer local context gained projection-only records.
    if (
        stage not in {"cluster_proposal", "gap_adjudication"}
        or current_context_hashes is None
    ):
        return False
    if not all(
        str(prior_hashes.get(component) or "")
        == str(current_component_hashes.get(component) or "")
        for component in _PROVIDER_INPUT_DEPENDENCY_COMPONENTS - {"context"}
    ):
        return False
    prior_context_hashes = checkpoint.get("dependency_context_hashes")
    if not isinstance(prior_context_hashes, Mapping):
        return False
    visible_context_components = (
        (
            "propositions",
            "relations",
            "topic_neighborhoods",
            "coverage_repair_source_ids",
            "coverage_focus_source_ids",
            "coverage_component_source_ids",
            "coverage_audit_mode",
            "coverage_component_signature",
            "current_clusters",
            "current_unclustered_sources",
            "prior_proposal_identities",
        )
        if stage == "cluster_proposal"
        else ("candidates", "internal_search_log")
    )
    return all(
        str(prior_context_hashes.get(component) or "")
        == str(current_context_hashes.get(component) or "")
        for component in visible_context_components
    )


def _checkpoint_dependency_context(value: Any, *, sort_sequences: bool = False) -> Any:
    """Remove projection-only churn while retaining semantic synthesis inputs."""

    if isinstance(value, Mapping):
        transient = {
            "active_cluster",
            "atomic_notes",
            "compatibility_path",
            "map_path",
            "path",
            "registry_status",
            "related_gap_ids",
            "source_contributions",
            "updated_at",
        }
        return {
            str(key): _checkpoint_dependency_context(
                child, sort_sequences=sort_sequences
            )
            for key, child in value.items()
            if str(key) not in transient
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        normalized = [
            _checkpoint_dependency_context(child, sort_sequences=sort_sequences)
            for child in value
        ]
        if sort_sequences:
            normalized.sort(
                key=lambda child: json.dumps(
                    child,
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        return normalized
    return value


_DIMENSION_ALIASES = {
    "theory": ("theory", "theories", "theoretical_framework", "theoretical_frameworks"),
    "mechanism": ("mechanism", "mechanisms"),
    "method": ("method", "methods", "methodology", "research_design"),
    "data": ("data", "dataset", "datasets", "data_source", "data_sources"),
    "case": ("case", "cases", "setting", "settings", "country", "countries"),
    "period": ("period", "periods", "time_period", "time_periods", "year", "years"),
    "outcome": ("outcome", "outcomes", "dependent_variable", "dependent_variables"),
    "finding direction": ("finding_direction", "direction", "effect_direction"),
    "uncertainty": (
        "uncertainty",
        "confidence",
        "precision",
        "qualification",
        "qualifications",
    ),
    "limitations": ("limitations", "limitation", "caveats", "caveat"),
}
_SEMANTIC_FIELDS = (
    "semantic_topics",
    "topics",
    "topic",
    "concepts",
    "key_concepts",
    "themes",
    "theme",
    "mechanisms",
    "mechanism",
    "theories",
    "theory",
    "outcomes",
    "outcome",
)
_STOPWORDS = {
    "a",
    "also",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "been",
    "being",
    "between",
    "by",
    "can",
    "could",
    "decrease",
    "decreased",
    "decreases",
    "did",
    "do",
    "does",
    "effect",
    "finding",
    "for",
    "four",
    "from",
    "grounded",
    "had",
    "has",
    "have",
    "how",
    "in",
    "include",
    "included",
    "includes",
    "including",
    "increase",
    "increased",
    "increases",
    "into",
    "is",
    "it",
    "made",
    "make",
    "makes",
    "may",
    "might",
    "more",
    "must",
    "negative",
    "not",
    "of",
    "on",
    "only",
    "or",
    "our",
    "page",
    "paper",
    "positive",
    "report",
    "reported",
    "research",
    "result",
    "see",
    "should",
    "source",
    "study",
    "studies",
    "than",
    "that",
    "the",
    "their",
    "then",
    "these",
    "this",
    "those",
    "to",
    "using",
    "via",
    "we",
    "what",
    "when",
    "where",
    "whether",
    "which",
    "with",
    "would",
}
_GENERIC_TOPIC_IDENTITIES = {
    "analytical",
    "document",
    "full",
    "none",
    "unknown",
    "unspecified",
}
_GENERIC_COMPARABILITY_TERMS = {
    "case",
    "civil",
    "conflict",
    "conflicts",
    "international",
    "internationalized",
    "intrastate",
    "mediation",
    "mediator",
    "number",
    "numbers",
    "outcome",
    "peace",
    "probability",
    "process",
    "rate",
    "share",
    "study",
    "war",
    "wars",
}
_OUTCOME_SIGNAL_TERMS = {
    "agreement",
    "casualty",
    "consent",
    "duration",
    "durability",
    "effectiveness",
    "implementation",
    "legitimacy",
    "negotiated",
    "onset",
    "participation",
    "relapse",
    "settlement",
    "stability",
    "success",
    "trust",
    "withdrawal",
}
_NON_DISCRIMINATING_RELATIONSHIP_TERMS = {
    "associated",
    "association",
    "correlated",
    "correlation",
    "create",
    "effect",
    "fewer",
    "greater",
    "higher",
    "improve",
    "improved",
    "improves",
    "lower",
    "longer",
    "negative",
    "negatively",
    "positive",
    "positively",
    "predict",
    "predicted",
    "relationship",
    "shorter",
    "significant",
    "significantly",
}
_WEAK_LOCATOR_MARKERS = {
    "",
    "unknown",
    "unavailable",
    "not reported",
    "n/a",
    "none",
    "not supplied",
}
_TRACEABLE_LOCATOR = re.compile(
    r"(?:\b(?:(?:p{1,2}\.\s*|p{1,2}\s+|pages?\s+|paragraphs?\s+|paras?\.?\s+)\d+(?:\s*[-\u2013\u2014]\s*\d+)?)\b|"
    r"\b(?:chapter|section|appendix)\s+[a-z0-9ivx.-]+\b|"
    r"\b(?:abstract|introduction|background|literature review|methods?|methodology|data|results?|findings?|"
    r"discussion|conclusions?|limitations?)\s*(?:section)?\b|\b(?:table|figure)\s*\d+[a-z]?\b)",
    flags=re.IGNORECASE,
)
_GENERATED_NOTE_LOCATOR = re.compile(
    r"\b(?:atomic note|detailed findings?|technical findings?|plain[- ]english findings?|"
    r"key findings?|automated validation|source lineage)\b(?:\s*\(\d+\))?",
    flags=re.IGNORECASE,
)
_PAGE_LOCATOR = re.compile(
    r"\b(?:(?:p{1,2}\.\s*|p{1,2}\s+|pages?\s+|paragraphs?\s+|paras?\.?\s+)\d+(?:\s*[-\u2013\u2014]\s*\d+)?)\b",
    flags=re.IGNORECASE,
)
_OBJECT_LOCATOR = re.compile(
    r"\b(?:table|figure|appendix)\s+[a-z0-9ivx.-]+\b", flags=re.IGNORECASE
)
_BROAD_SECTION_ONLY_LOCATOR = re.compile(
    r"^(?:abstract|introduction|background|literature review|methods?|methodology|"
    r"data|results?|findings?|discussion|conclusions?|limitations?)\s*(?:section)?$",
    flags=re.IGNORECASE,
)
_AUTHOR_GAP_MARKER = re.compile(
    r"\b(?:future|further|unknown|unresolved|understudied|unexplored|"
    r"remain(?:s|ed)?\s+(?:unclear|unknown|untested|unresolved)|"
    r"not\s+(?:examined|tested|assessed|explored|known|identified)|"
    r"lack(?:s|ing)?\s+(?:of\s+)?(?:evidence|research|data|studies|testing)|"
    r"no\s+(?:empirical\s+)?(?:evidence|research|study|studies|test|testing|replication)|"
    r"need(?:s|ed)?\s+(?:for|to)|should|"
    r"require(?:s|d)?\s+(?:further|additional|testing|research|evidence)|"
    r"replicat(?:e|es|ed|ion)|research\s+gap)\b",
    flags=re.IGNORECASE,
)


def _as_mapping(value: Any) -> dict[str, Any]:
    """Accept mappings and the incoming dataclass models without importing them."""
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value):
        return dict(asdict(value))
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        if isinstance(result, Mapping):
            return dict(result)
    result: dict[str, Any] = {}
    for name in getattr(value, "__slots__", ()):
        if hasattr(value, name):
            result[str(name)] = getattr(value, name)
    if result:
        return result
    try:
        return dict(vars(value))
    except (TypeError, AttributeError):
        raise TypeError(
            f"expected a mapping or serializable model, got {type(value).__name__}"
        ) from None


def _policy_value(policy: Any, names: str | Sequence[str], default: Any) -> Any:
    values = _as_mapping(policy) if policy is not None else {}
    for name in (names,) if isinstance(names, str) else names:
        if name in values and values[name] is not None:
            return values[name]
    return default


def _stable_hash(value: Any) -> str:
    return sha256_text(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    )


def _flatten_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, Mapping):
        for key in ("value", "name", "label", "text", "title", "description"):
            if value.get(key):
                return _flatten_values(value[key])
        rows: list[str] = []
        for item in value.values():
            rows.extend(_flatten_values(item))
        return rows
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        rows = []
        for item in value:
            rows.extend(_flatten_values(item))
        return rows
    return [str(value)]


def _stem_token(value: str) -> str:
    if len(value) > 5 and value.endswith("ies"):
        return value[:-3] + "y"
    if len(value) > 5 and value.endswith("sses"):
        return value[:-2]
    if (
        len(value) > 4
        and value.endswith("s")
        and not value.endswith(("ss", "is", "us"))
    ):
        return value[:-1]
    return value


def _tokens(value: Any) -> set[str]:
    text = " ".join(_flatten_values(value)).casefold()
    return {
        _stem_token(token)
        for token in re.findall(r"[a-z0-9]+", text)
        if token not in _STOPWORDS and len(token) > 2
    }


def _canonical_phrase(value: Any) -> str:
    return " ".join(sorted(_tokens(value)))


def _author_gap_record(value: Any, *, origin: str) -> dict[str, Any] | None:
    item = (
        _as_mapping(value)
        if not isinstance(value, str)
        else {"missing_evidence": value}
    )
    text = str(
        item.get("precise_missing_evidence")
        or item.get("missing_evidence")
        or item.get("gap_text")
        or item.get("text")
        or ""
    ).strip()
    if len(_tokens(text)) < 2:
        return None
    if origin != "future_research" and not _AUTHOR_GAP_MARKER.search(text):
        return None
    return {**item, "missing_evidence": text, "_author_gap_origin": origin}


def _dimension_values(row: Mapping[str, Any], dimension: str) -> list[str]:
    values: list[str] = []
    for alias in _DIMENSION_ALIASES[dimension]:
        if alias in row:
            values.extend(_flatten_values(row.get(alias)))
    normalized = []
    for value in values:
        clean = re.sub(r"\s+", " ", value).strip()
        if clean and clean.casefold() not in {item.casefold() for item in normalized}:
            normalized.append(clean)
    return normalized


def _locator_text(value: Any) -> str:
    if isinstance(value, Mapping):
        ordered = [
            value.get(key)
            for key in (
                "page",
                "pages",
                "section",
                "paragraph",
                "table",
                "figure",
                "quote",
            )
        ]
        text = "; ".join(item for item in _flatten_values(ordered) if item)
        return text or "; ".join(_flatten_values(value))
    values = _flatten_values(value)
    return "; ".join(values[:3])


def _complete_locator(value: Any) -> bool:
    text = _locator_text(value).strip().casefold()
    return bool(
        text
        and text not in _WEAK_LOCATOR_MARKERS
        and not any(marker in text for marker in ("not supplied", "not available"))
        and _TRACEABLE_LOCATOR.search(text)
        and not _GENERATED_NOTE_LOCATOR.search(text)
    )


def _source_locator(value: Any) -> dict[str, Any]:
    """Normalize a locator and distinguish source-native from generated note headings."""

    text = _locator_text(value).strip()
    normalized = _normalized_locator(text)
    if not text or text.casefold() in _WEAK_LOCATOR_MARKERS:
        kind = "missing"
    elif _GENERATED_NOTE_LOCATOR.search(text):
        kind = "generated_note_heading"
    elif _PAGE_LOCATOR.search(text):
        kind = "page_or_paragraph"
    elif _OBJECT_LOCATOR.search(text):
        kind = "source_object"
    elif _BROAD_SECTION_ONLY_LOCATOR.fullmatch(text):
        kind = "broad_section_heading"
    elif _TRACEABLE_LOCATOR.search(text):
        kind = "source_heading"
    else:
        kind = "untyped_text"
    return {
        "raw": text,
        "normalized": normalized,
        "kind": kind,
        "traceable": kind in {"page_or_paragraph", "source_object", "source_heading"},
        "strong_synthesis_support": kind
        in {"page_or_paragraph", "source_object", "source_heading"},
        "rejection_reason": "generated_note_heading"
        if kind == "generated_note_heading"
        else "",
    }


def _explicit_source_locator(value: Any) -> dict[str, Any]:
    """Normalize a typed locator already validated in an evidence-profile sidecar."""

    row = _as_mapping(value)
    raw = _locator_text(row.get("value") or row)
    locator_type = str(row.get("locator_type") or "unknown")
    kind = {
        "page": "page_or_paragraph",
        "page_range": "page_or_paragraph",
        "paragraph": "page_or_paragraph",
        "table": "source_object",
        "figure": "source_object",
        "chapter": "source_heading",
        "source_heading": "source_heading",
        "quote_span": "source_heading",
        "generated_heading": "generated_note_heading",
    }.get(locator_type, "untyped_text")
    source_native = bool(row.get("source_native"))
    strong = bool(row.get("supports_strong_assertion")) and source_native
    return {
        "raw": raw,
        "normalized": _normalized_locator(raw),
        "kind": kind,
        "traceable": bool(raw)
        and source_native
        and kind
        not in {
            "generated_note_heading",
            "untyped_text",
        },
        "strong_synthesis_support": bool(raw)
        and strong
        and kind
        not in {
            "generated_note_heading",
            "untyped_text",
        },
        "rejection_reason": "" if strong else "source_native_locator_required",
    }


def _normalized_locator(value: Any) -> str:
    text = _locator_text(value).casefold().replace("\u2013", "-").replace("\u2014", "-")
    text = re.sub(r"\b(pp?)\.\s+(?=\d)", r"\1.", text)
    return re.sub(r"\s+", " ", text).strip(" .;,:")


def _reference_matches_profile(
    reference: Mapping[str, Any], profile: Mapping[str, Any]
) -> bool:
    """Require a reasoner reference to resolve to an existing located anchor."""
    if str(reference.get("source_id") or "") != str(profile.get("source_id") or ""):
        return False
    anchor_id = str(
        reference.get("evidence_anchor_id")
        or reference.get("claim_id")
        or reference.get("finding_id")
        or ""
    )
    if not anchor_id:
        return False
    claim = next(
        (
            row
            for row in profile.get("claims", []) or []
            if str(row.get("evidence_anchor_id") or row.get("claim_id") or "")
            == anchor_id
        ),
        None,
    )
    if claim is None or not claim.get("locator_complete"):
        return False
    locator = reference.get("locator") or claim.get("locator")
    return _complete_locator(locator) and _normalized_locator(
        locator
    ) == _normalized_locator(claim.get("locator"))


def _reference_is_synthesis_eligible(
    reference: Mapping[str, Any],
    profile: Mapping[str, Any],
) -> bool:
    if not _reference_matches_profile(reference, profile):
        return False
    anchor_id = str(
        reference.get("evidence_anchor_id")
        or reference.get("claim_id")
        or reference.get("finding_id")
        or ""
    )
    return any(
        str(anchor.get("evidence_anchor_id") or anchor.get("claim_id") or "")
        == anchor_id
        and _anchor_is_synthesis_eligible(anchor)
        for anchor in profile.get("claims", []) or []
    )


def _proposal_membership_evidence(
    profile: Mapping[str, Any],
    proposal: Mapping[str, Any],
) -> dict[str, Any]:
    """Attach one exact located claim when a concise proposal omits member evidence."""

    located_claims = [
        claim
        for claim in profile.get("claims", []) or []
        if claim.get("locator_complete")
    ]
    if not located_claims:
        return {}
    proposal_terms = _tokens(
        [
            proposal.get("semantic_identity"),
            proposal.get("label"),
            proposal.get("shared_question"),
            proposal.get("coherence_rationale"),
        ]
    )

    def claim_score(claim: Mapping[str, Any]) -> tuple[int, str]:
        claim_terms = _tokens(
            [
                claim.get("text"),
                claim.get("topic"),
                claim.get("dimensions"),
            ]
        )
        return (len(proposal_terms & claim_terms), str(claim.get("claim_id") or ""))

    selected = max(located_claims, key=claim_score)
    return _evidence_ref(selected)


def _normalize_direction(value: Any) -> str:
    text = " ".join(_flatten_values(value)).casefold().strip()
    if not text:
        return "not_reported"
    # Statistical nulls take precedence over the sign of an imprecise point
    # estimate. "Negative (not significant)" is null evidence, not a mapped
    # negative position in a debate.
    if any(
        marker in text
        for marker in ("null", "no effect", "no association", "not significant")
    ):
        return "null"
    if any(
        marker in text
        for marker in ("positive", "increase", "higher", "supports", "improves")
    ):
        return "positive"
    if any(
        marker in text
        for marker in ("negative", "decrease", "lower", "undermines", "reduces")
    ):
        return "negative"
    if any(
        marker in text for marker in ("mixed", "conditional", "heterogeneous", "varies")
    ):
        return "mixed"
    return slugify(text, "not-reported").replace("-", "_")


def _normalized_support_envelope(
    item: Mapping[str, Any], raw: Mapping[str, Any]
) -> dict[str, Any]:
    supplied = item.get("support_envelope")
    envelope = dict(supplied) if isinstance(supplied, Mapping) else {}
    empirical_role = str(
        envelope.get("empirical_role") or item.get("empirical_role") or "none"
    ).casefold()
    argument_role = str(
        envelope.get("argument_role") or item.get("argument_role") or "none"
    ).casefold()
    finding_type = str(item.get("finding_type") or "").casefold()
    claim_text = str(
        item.get("claim") or item.get("finding") or item.get("text") or ""
    ).casefold()
    if empirical_role not in {*EMPIRICAL_SUPPORT_ROLES, "none"}:
        empirical_role = "none"
    if argument_role not in {*ARGUMENT_SUPPORT_ROLES, "none"}:
        argument_role = "none"
    if empirical_role == "none" and argument_role == "none":
        if any(token in finding_type for token in ("causal", "experiment", "quasi")):
            empirical_role = "causal"
        elif "mechanism" in finding_type or "process tracing" in claim_text:
            empirical_role = "mechanism_evidence"
        elif any(
            token in finding_type
            for token in ("association", "correlation", "regression", "statistical")
        ):
            empirical_role = "associational"
        elif any(
            token in finding_type
            for token in ("descriptive", "qualitative", "empirical")
        ):
            empirical_role = "descriptive"
        elif any(token in finding_type for token in ARGUMENT_SUPPORT_ROLES):
            argument_role = next(
                token for token in ARGUMENT_SUPPORT_ROLES if token in finding_type
            )
        elif claim_text and _complete_locator(
            item.get("locator") or item.get("locators")
        ):
            # Legacy analytical rows did not declare evidence roles. Treat the
            # located statement as descriptive support, never causal support.
            empirical_role = "descriptive"

    coverage = str(envelope.get("coverage") or item.get("coverage") or "").casefold()
    if not coverage:
        source_coverage = (
            raw.get("coverage") if isinstance(raw.get("coverage"), Mapping) else {}
        )
        if source_coverage.get("full_document") is True:
            coverage = "full_text"
        elif (
            str(raw.get("note_status") or raw.get("status") or "").casefold()
            in ANALYTICAL_STATUSES
        ):
            coverage = "full_text"
        else:
            coverage = "unknown"
    if coverage not in {"full_text", "limited_text", "abstract", "metadata", "unknown"}:
        coverage = "unknown"
    scope = envelope.get("scope") if isinstance(envelope.get("scope"), Mapping) else {}
    restrictions = [
        str(value)
        for value in envelope.get("restrictions", item.get("restrictions", [])) or []
        if str(value).strip()
    ]
    source_kind = " ".join(
        _flatten_values(
            [
                raw.get("source_role"),
                raw.get("methods"),
                raw.get("title"),
                _as_mapping(raw.get("context")).get("title"),
                _as_mapping(raw.get("context")).get("event"),
            ]
        )
    ).casefold()
    qualitative_comparison = bool(
        re.search(
            r"\b(?:structured,? focused comparison|comparative case stud(?:y|ies)|"
            r"case stud(?:y|ies)|small[- ]n|qualitative comparison)\b",
            source_kind,
        )
    )
    explicit_causal_identification = bool(
        re.search(
            r"\b(?:process tracing|natural experiment|randomi[sz]ed|instrumental variable|"
            r"regression discontinuity|difference[- ]in[- ]differences|synthetic control)\b",
            source_kind,
        )
    )
    if empirical_role == "causal" and qualitative_comparison and not explicit_causal_identification:
        empirical_role = "mechanism_evidence"
        restrictions.append(
            "The qualitative comparison supports a case-bound mechanism interpretation, not a general causal-effect estimate."
        )
    restriction_text = " ".join(restrictions).casefold()
    opinion_report = bool(
        re.search(r"\b(?:conference|practitioner|workshop|proceedings)\b", source_kind)
        and re.search(
            r"\b(?:panelist|participant) opinions?\b|\bnot empirical evidence\b|"
            r"\bno original research data\b|\bqualitative summary of discussions\b",
            restriction_text,
        )
    )
    if opinion_report and empirical_role in {"none", "descriptive"}:
        # Conference summaries can establish what practitioners recommended or
        # discussed. They cannot become independent effectiveness studies merely
        # because the recommendation uses result-like language.
        empirical_role = "none"
        argument_role = "practitioner_guidance"
    support_status = str(
        envelope.get("support_status") or item.get("support_status") or ""
    ).casefold()
    if support_status not in {"supported", "support_unknown", "limited", "unsupported"}:
        support_status = (
            "supported"
            if coverage == "full_text"
            and (empirical_role != "none" or argument_role != "none")
            else "support_unknown"
        )
    return {
        "empirical_role": empirical_role,
        "argument_role": argument_role,
        "coverage": coverage,
        "scope": {
            str(key): [str(value) for value in values or [] if str(value).strip()]
            for key, values in scope.items()
            if isinstance(values, Sequence)
            and not isinstance(values, (str, bytes, bytearray))
        },
        "restrictions": restrictions,
        "support_status": support_status,
    }


def _anchor_evidence_role(item: Mapping[str, Any], envelope: Mapping[str, Any]) -> str:
    if (
        str(envelope.get("empirical_role") or "none") == "none"
        and str(envelope.get("argument_role") or "none")
        == "practitioner_guidance"
    ):
        return "practitioner_guidance"
    supplied = str(item.get("evidence_role") or "").strip().casefold()
    if supplied:
        return supplied
    if str(envelope.get("empirical_role") or "none") != "none":
        return str(envelope["empirical_role"])
    if str(envelope.get("argument_role") or "none") != "none":
        return str(envelope["argument_role"])
    return "support_unknown"


def _stable_evidence_anchor_id(
    source_id: str,
    locator: str,
    evidence_role: str,
    item: Mapping[str, Any],
) -> str:
    supplied = str(
        item.get("evidence_anchor_id")
        or item.get("claim_id")
        or item.get("finding_id")
        or ""
    )
    if supplied:
        return supplied
    source_span = str(
        item.get("source_span") or item.get("span") or item.get("quote") or ""
    )
    identity = {
        "source_id": source_id,
        "locator": _normalized_locator(locator),
        "evidence_role": evidence_role,
        "source_span_hash": _stable_hash(source_span)[:12] if source_span else "",
    }
    return f"anchor-{_stable_hash(identity)[:16]}"


def _normalize_claims(
    raw: Mapping[str, Any], source_id: str, family_id: str
) -> list[dict[str, Any]]:
    candidates: list[Any] = []
    candidate_fields = (
        # Profiles may already contain a selected anchor set while retaining
        # more precise structured findings. Keep both available to map-local
        # proposition admission: otherwise a broad table-summary anchor can
        # hide the exact row that tests the proposed relationship.
        ("evidence_anchors", "findings")
        if raw.get("evidence_anchors")
        else ("claims", "structured_findings", "findings", "evidence_records")
    )
    for key in candidate_fields:
        value = raw.get(key)
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            candidates.extend(value)
        elif value:
            candidates.append(value)
    if not candidates:
        has_dimensions = any(
            _dimension_values(raw, dimension) for dimension in EVIDENCE_DIMENSIONS
        )
        if has_dimensions or raw.get("locator") or raw.get("locators"):
            candidates.append(raw)
    claims: list[dict[str, Any]] = []
    for candidate in candidates[:24]:
        item = (
            _as_mapping(candidate)
            if not isinstance(candidate, str)
            else {"finding": candidate}
        )
        locator = _locator_text(
            item.get("locator")
            or item.get("locators")
            or raw.get("locator")
            or raw.get("locators")
        )
        explicit_locators = [
            _explicit_source_locator(value)
            for value in item.get("source_locators", []) or []
            if isinstance(value, Mapping)
        ]
        explicit_locators.sort(
            key=lambda value: (
                not bool(value.get("strong_synthesis_support")),
                {"page_or_paragraph": 0, "source_object": 1, "source_heading": 2}.get(
                    str(value.get("kind") or ""),
                    9,
                ),
                str(value.get("raw") or ""),
            )
        )
        # Source-level dimensions are retrieval metadata only. They must not be
        # copied into every anchor; doing so creates false evidence cells.
        dimensions = {
            dimension: _dimension_values(item, dimension)
            for dimension in EVIDENCE_DIMENSIONS
        }
        direction = _normalize_direction(
            dimensions["finding direction"]
            or item.get("direction")
            or item.get("finding_direction")
        )
        dimensions["finding direction"] = (
            [] if direction == "not_reported" else [direction]
        )
        text = str(
            item.get("claim")
            or item.get("finding")
            or item.get("text")
            or item.get("description")
            or ""
        ).strip()
        envelope = _normalized_support_envelope(item, raw)
        evidence_role = _anchor_evidence_role(item, envelope)
        anchor_id = _stable_evidence_anchor_id(source_id, locator, evidence_role, item)
        source_locator = (
            explicit_locators[0]
            if explicit_locators and explicit_locators[0].get("traceable")
            else _source_locator(locator)
        )
        if source_locator.get("raw"):
            locator = str(source_locator["raw"])
        quantitative_raw = (
            item.get("quantitative_results")
            or (
                [item.get("quantitative_result")]
                if isinstance(item.get("quantitative_result"), Mapping)
                else []
            )
            or item.get("statistics")
            or item.get("estimates")
            or []
        )
        quantitative_results = [
            _as_mapping(row) for row in quantitative_raw if isinstance(row, Mapping)
        ]
        claims.append(
            {
                "evidence_anchor_id": anchor_id,
                # Internal compatibility alias. New persisted references also
                # include evidence_anchor_id and readers prefer it.
                "claim_id": anchor_id,
                "source_id": source_id,
                "study_family_id": family_id,
                "text": text,
                "locator": locator,
                "locator_complete": bool(source_locator["traceable"]),
                "source_locator": source_locator,
                "dimensions": dimensions,
                "direction": direction,
                "topic": str(item.get("topic") or raw.get("topic") or ""),
                "plain_english_meaning": str(
                    item.get("plain_english_meaning") or item.get("plain_english") or ""
                ),
                "magnitude": str(item.get("magnitude") or item.get("estimate") or ""),
                "comparison": str(item.get("comparison") or ""),
                "uncertainty": str(item.get("uncertainty") or ""),
                "quantitative_results": quantitative_results,
                "evidence_role": evidence_role,
                "support_envelope": envelope,
                "support_status": str(
                    envelope.get("support_status") or "support_unknown"
                ),
                "boundary_condition": str(
                    item.get("boundary_condition")
                    or item.get("boundary")
                    or "; ".join(_flatten_values(item.get("conditions")))
                ),
                "mechanism_tested": item.get("mechanism_tested"),
                "addresses_gap": item.get("addresses_gap", False),
                "gap_rule": str(item.get("gap_rule") or ""),
                "answer_status": str(item.get("answer_status") or ""),
            }
        )
    return sorted(
        {
            _stable_hash([row["source_id"], row["evidence_anchor_id"]]): row
            for row in claims
        }.values(),
        key=lambda row: row["evidence_anchor_id"],
    )


def _topic_scores(
    raw: Mapping[str, Any], claims: Sequence[Mapping[str, Any]]
) -> dict[str, float]:
    scores: dict[str, float] = {}
    weights = {
        "semantic_topics": 1.0,
        "topics": 1.0,
        "topic": 1.0,
        "concepts": 0.9,
        "key_concepts": 0.9,
        "themes": 0.85,
        "theme": 0.85,
        "mechanisms": 0.8,
        "mechanism": 0.8,
        "theories": 0.7,
        "theory": 0.7,
        "outcomes": 0.65,
        "outcome": 0.65,
    }
    for field in _SEMANTIC_FIELDS:
        for value in _flatten_values(raw.get(field)):
            phrase = _canonical_phrase(value)
            if phrase and not set(phrase.split()).issubset(_GENERIC_TOPIC_IDENTITIES):
                scores[phrase] = max(scores.get(phrase, 0.0), weights[field])
    for claim in claims:
        phrase = _canonical_phrase(claim.get("topic"))
        if phrase and not set(phrase.split()).issubset(_GENERIC_TOPIC_IDENTITIES):
            scores[phrase] = max(scores.get(phrase, 0.0), 0.95)
        for dimension in ("mechanism", "theory", "outcome"):
            for value in claim.get("dimensions", {}).get(dimension, []) or []:
                phrase = _canonical_phrase(value)
                if phrase and not set(phrase.split()).issubset(
                    _GENERIC_TOPIC_IDENTITIES
                ):
                    scores[phrase] = max(scores.get(phrase, 0.0), 0.7)
    if not scores:
        title_tokens = sorted(_tokens(raw.get("title", "")))
        for token in title_tokens:
            scores[token] = 0.45
    return dict(sorted(scores.items()))


def _topic_labels(
    raw: Mapping[str, Any],
    claims: Sequence[Mapping[str, Any]],
    scores: Mapping[str, float],
) -> dict[str, str]:
    candidates: dict[str, Counter[str]] = defaultdict(Counter)

    def add(value: Any) -> None:
        for flattened in _flatten_values(value):
            label = re.sub(r"\s+", " ", flattened).strip(" .;:,-")
            identity = _canonical_phrase(label)
            if identity in scores and label:
                candidates[identity][label] += 1

    for field in _SEMANTIC_FIELDS:
        add(raw.get(field))
    for claim in claims:
        add(claim.get("topic"))
        dimensions = (
            claim.get("dimensions", {})
            if isinstance(claim.get("dimensions"), Mapping)
            else {}
        )
        for dimension in ("mechanism", "theory", "outcome"):
            add(dimensions.get(dimension))
    return {
        identity: sorted(
            labels, key=lambda label: (-labels[label], len(label), label.casefold())
        )[0]
        for identity, labels in sorted(candidates.items())
        if labels
    }


def _lineage_values(value: Any) -> list[str]:
    return sorted(
        {
            _canonical_phrase(row) or str(row).casefold()
            for row in _flatten_values(value)
            if str(row).strip()
        }
    )


def _lineage_fieldwork_signals(*values: Any) -> list[str]:
    """Extract conservative reusable-fieldwork signals from profile prose.

    Exact data-source strings vary across profiles (for example, one record may
    say "41 semi-structured interviews" while another embeds the same count in
    a longer description). A matching interview count is only a candidate
    overlap signal; reconciliation additionally requires a shared author.
    """

    text = " ".join(_flatten_values(values))
    signals: set[str] = set()
    patterns = (
        r"\b(?P<count>\d{1,4})\s+(?:(?:semi[- ]structured|structured|elite|expert|key[- ]informant|qualitative)\s+)?interviews?\b",
        r"\binterviews?\s+(?:with\s+)?(?P<count>\d{1,4})\b",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.I):
            count = int(match.group("count"))
            if count >= 5:
                signals.add(f"interviews:{count}")
    return sorted(signals)


def _normalized_study_lineage(
    raw: Mapping[str, Any],
    *,
    source_id: str,
    family_id: str,
    doi: str,
) -> dict[str, Any]:
    supplied = _as_mapping(raw.get("study_lineage"))
    source_role = str(raw.get("source_role") or "").casefold()
    institution = str(
        supplied.get("institution")
        or next(iter(_flatten_values(supplied.get("institutions"))), "")
        or supplied.get("institutional_series")
        or raw.get("institution")
        or raw.get("issuing_institution")
        or raw.get("publisher")
        or ""
    ).strip()
    program_ids = _lineage_values(
        supplied.get("program_ids")
        or supplied.get("program_id")
        or supplied.get("institutional_series")
        or raw.get("research_program")
        or raw.get("program_id")
    )
    dataset_ids = _lineage_values(
        supplied.get("dataset_ids")
        or supplied.get("dataset_id")
        or supplied.get("datasets")
        or supplied.get("data_sources")
        or raw.get("datasets")
        or raw.get("dataset")
    )
    sample_ids = _lineage_values(
        supplied.get("sample_ids")
        or supplied.get("sample_id")
        or supplied.get("sampling_frame")
        or raw.get("sample_id")
        or raw.get("fieldwork_id")
    )
    fieldwork_signals = _lineage_fieldwork_signals(
        supplied.get("data_sources"),
        supplied.get("datasets"),
        supplied.get("sampling_frame"),
        raw.get("data_sources"),
        raw.get("datasets"),
        dataset_ids,
        sample_ids,
    )
    publication_id = str(
        supplied.get("publication_id") or (f"doi:{doi}" if doi else source_id)
    )
    explicit_group = str(
        supplied.get("evidence_base_group_id")
        or raw.get("evidence_base_group_id")
        or ""
    ).strip()
    supplied_family = str(
        supplied.get("study_family_id")
        or raw.get("study_family_id")
        or raw.get("study_id")
        or ""
    ).strip()
    substantive_family = (
        supplied_family
        if supplied_family
        and not supplied_family.casefold().startswith(("doi:", "title:"))
        and supplied_family != source_id
        else ""
    )
    explicit_group_basis = str(supplied.get("group_basis") or "").strip()
    if explicit_group and explicit_group_basis in {
        "shared_sample_or_fieldwork",
        "shared_dataset",
        "shared_research_program",
        "study_family",
        "institutional_guidance_program",
    }:
        group_basis = "explicit_evidence_base_group"
        group_identity: Any = explicit_group
    elif sample_ids:
        group_basis = "shared_sample_or_fieldwork"
        group_identity = sample_ids
    elif dataset_ids:
        group_basis = "shared_dataset"
        group_identity = dataset_ids
    elif program_ids:
        group_basis = "shared_research_program"
        group_identity = program_ids
    elif substantive_family:
        group_basis = "study_family"
        group_identity = substantive_family
    elif institution and any(
        marker in source_role
        for marker in ("practitioner", "guidance", "institutional")
    ):
        group_basis = "institutional_guidance_program"
        group_identity = _canonical_phrase(institution)
    else:
        # A publication identity proves that a record exists; it does not
        # prove that its sample, dataset, fieldwork, or institutional evidence
        # is independent from another publication.
        group_basis = "independence_uncertain"
        group_identity = publication_id
    group_id = (
        explicit_group
        or f"evidence-base-{_stable_hash([group_basis, group_identity])[:16]}"
    )
    counted_as_independent = group_basis != "independence_uncertain"
    return {
        "lineage_id": str(
            supplied.get("lineage_id")
            or supplied.get("study_lineage_id")
            or f"lineage-{_stable_hash(source_id)[:16]}"
        ),
        "source_id": source_id,
        "publication_id": publication_id,
        "study_family_id": family_id,
        "evidence_base_group_id": group_id,
        "group_basis": group_basis,
        "program_ids": program_ids,
        "dataset_ids": dataset_ids,
        "sample_ids": sample_ids,
        "fieldwork_signals": fieldwork_signals,
        "institution": institution,
        "authors": [
            str(value)
            for value in supplied.get("authors", []) or []
            if str(value).strip()
        ],
        "populations": [
            str(value)
            for value in supplied.get("populations", []) or []
            if str(value).strip()
        ],
        "periods": [
            str(value)
            for value in supplied.get("periods", []) or []
            if str(value).strip()
        ],
        "overlap_signals": [
            str(value)
            for value in supplied.get("overlap_signals", []) or []
            if str(value).strip()
        ],
        "publication_relationships": [
            _as_mapping(value)
            for value in supplied.get("publication_relationships", []) or []
            if isinstance(value, Mapping)
        ],
        "independence_status": (
            "independent_evidence_base"
            if counted_as_independent
            else "independence_uncertain"
        ),
        "counted_as_independent": counted_as_independent,
        "confidence": str(
            supplied.get("confidence")
            or ("moderate" if counted_as_independent else "unknown")
        ),
        "version": STUDY_LINEAGE_VERSION,
    }


def build_independence_records(profiles: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Return strict public lineage records and conservative effective-base accounting."""

    rows = [dict(row) for row in profiles]
    strict_lineages: list[dict[str, Any]] = []
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for profile in rows:
        lineage = _as_mapping(profile.get("study_lineage"))
        source_id = str(profile.get("source_id") or "")
        lineage_id = str(
            lineage.get("lineage_id") or f"lineage-{_stable_hash(source_id)[:16]}"
        )
        confidence = str(lineage.get("confidence") or "unknown").casefold()
        if confidence == "medium":
            confidence = "moderate"
        if confidence not in {"high", "moderate", "low", "unknown"}:
            confidence = "unknown"
        strict_lineages.append(
            {
                "study_lineage_id": lineage_id,
                "source_ids": [source_id] if source_id else [],
                "authors": [
                    str(value)
                    for value in lineage.get("authors", []) or []
                    if str(value)
                ],
                "institutions": [str(lineage.get("institution"))]
                if lineage.get("institution")
                else [],
                "datasets": [
                    str(value)
                    for value in lineage.get("dataset_ids", []) or []
                    if str(value)
                ],
                "data_sources": [
                    str(value)
                    for value in lineage.get("dataset_ids", []) or []
                    if str(value)
                ],
                "sampling_frame": "; ".join(
                    str(value)
                    for value in lineage.get("sample_ids", []) or []
                    if str(value)
                ),
                "unit_of_analysis": str(lineage.get("unit_of_analysis") or ""),
                "populations": [
                    str(value)
                    for value in lineage.get("populations", []) or []
                    if str(value)
                ],
                "periods": [
                    str(value)
                    for value in lineage.get("periods", []) or []
                    if str(value)
                ],
                "publication_relationships": [
                    _as_mapping(value)
                    for value in lineage.get("publication_relationships", []) or []
                    if isinstance(value, Mapping)
                ],
                "institutional_series": "; ".join(
                    str(value)
                    for value in lineage.get("program_ids", []) or []
                    if str(value)
                ),
                "overlap_signals": sorted(
                    {
                        *[
                            str(value)
                            for value in lineage.get("overlap_signals", []) or []
                            if str(value)
                        ],
                        *(
                            [str(lineage.get("group_basis"))]
                            if lineage.get("group_basis")
                            and lineage.get("group_basis") != "independence_uncertain"
                            else []
                        ),
                    }
                ),
                "confidence": confidence,
            }
        )
        group_id = str(lineage.get("evidence_base_group_id") or "")
        if group_id:
            groups[group_id].append(profile)

    lineage_id_by_source = {
        str(source_id): str(lineage["study_lineage_id"])
        for lineage in strict_lineages
        for source_id in lineage["source_ids"]
    }
    group_rows: list[dict[str, Any]] = []
    assessments: list[dict[str, Any]] = []
    for group_id, members in sorted(groups.items()):
        lineages = [_as_mapping(row.get("study_lineage")) for row in members]
        source_ids = sorted(
            str(row.get("source_id") or "") for row in members if row.get("source_id")
        )
        bases = sorted(
            {
                str(row.get("group_basis") or "")
                for row in lineages
                if row.get("group_basis")
            }
        )
        overlap_signals = sorted(
            {
                *bases,
                *[
                    str(signal)
                    for lineage in lineages
                    for signal in lineage.get("overlap_signals", []) or []
                    if str(signal)
                ],
            }
        )
        counted = all(
            lineage.get("counted_as_independent") is True for lineage in lineages
        )
        if not counted:
            relationship = "independence_uncertain"
        elif any("institutional_guidance" in basis for basis in bases):
            relationship = "institutional_series"
        elif len(source_ids) > 1 and any("study_family" in basis for basis in bases):
            relationship = "same_study"
        elif len(source_ids) > 1:
            relationship = "overlapping_evidence_base"
        else:
            relationship = "independent_evidence_base"
        rationale = (
            "Independence is unresolved because the profile contains no usable sample, dataset, program, or study-family lineage."
            if relationship == "independence_uncertain"
            else "Publications are counted once for this shared evidence base."
            if len(source_ids) > 1
            else "The available lineage identifies one countable evidence base."
        )
        group_rows.append(
            {
                "evidence_base_group_id": group_id,
                "proposition_id": "",
                "source_ids": source_ids,
                "study_lineage_ids": [
                    lineage_id_by_source[source_id] for source_id in source_ids
                ],
                "relationship": relationship,
                "counted_as_independent": counted,
                "rationale": rationale,
                "overlap_signals": overlap_signals,
            }
        )
        assessments.append(
            {
                "assessment_id": f"independence-{_stable_hash([group_id, source_ids])[:16]}",
                "proposition_id": "",
                "source_ids": source_ids,
                "evidence_base_group_ids": [group_id],
                "status": relationship,
                "effective_evidence_base_count": 1 if counted else 0,
                "rationale": rationale,
                "overlap_signals": overlap_signals,
                "confidence": "unknown" if not counted else "moderate",
                "evidence": [],
            }
        )
    return {
        "study_lineages": sorted(strict_lineages, key=lambda row: row["source_ids"]),
        "independence_assessments": assessments,
        "evidence_base_groups": group_rows,
    }


def _reconcile_evidence_base_groups(rows: list[dict[str, Any]]) -> None:
    """Union publications only on explicit, study-level overlap signals."""

    parents = list(range(len(rows)))
    reasons_by_root: dict[int, set[str]] = defaultdict(set)

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int, reasons: Sequence[str]) -> None:
        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            reasons_by_root[left_root].update(reasons)
            return
        keep, merge = sorted((left_root, right_root))
        parents[merge] = keep
        reasons_by_root[keep].update(reasons_by_root.pop(merge, set()))
        reasons_by_root[keep].update(reasons)

    def values(lineage: Mapping[str, Any], key: str) -> set[str]:
        return {
            _canonical_phrase(value) or str(value).casefold()
            for value in lineage.get(key, []) or []
            if str(value)
        }

    generic_lineage_values = {
        "archive",
        "case study",
        "document analysis",
        "interview",
        "observation",
        "secondary data",
        "survey",
    }
    for left_index, right_index in combinations(range(len(rows)), 2):
        left, right = rows[left_index], rows[right_index]
        left_lineage = _as_mapping(left.get("study_lineage"))
        right_lineage = _as_mapping(right.get("study_lineage"))
        reasons: list[str] = []
        left_family = str(left.get("study_family_id") or "")
        right_family = str(right.get("study_family_id") or "")
        family_is_substantive = (
            left_family
            and not left_family.casefold().startswith(("doi:", "title:"))
            and left_family
            not in {
                str(left.get("source_id") or ""),
                str(right.get("source_id") or ""),
            }
        )
        if family_is_substantive and left_family == right_family:
            reasons.append(f"study_family:{left_family}")
        for key, label in (
            ("sample_ids", "shared_sample"),
            ("dataset_ids", "shared_dataset"),
            ("program_ids", "shared_program"),
        ):
            shared = values(left_lineage, key) & values(right_lineage, key)
            shared -= generic_lineage_values
            if key == "dataset_ids":
                shared = {
                    value
                    for value in shared
                    if not re.search(
                        r"\b(?:interviews?|fieldwork|document analysis|secondary data)\b",
                        value,
                        flags=re.I,
                    )
                }
            reasons.extend(f"{label}:{value}" for value in sorted(shared))
        shared_fieldwork = {
            str(value).casefold().strip()
            for value in left_lineage.get("fieldwork_signals", []) or []
            if str(value).strip()
        } & {
            str(value).casefold().strip()
            for value in right_lineage.get("fieldwork_signals", []) or []
            if str(value).strip()
        }
        left_authors = values(left_lineage, "authors")
        right_authors = values(right_lineage, "authors")
        if shared_fieldwork and left_authors & right_authors:
            reasons.extend(
                f"shared_fieldwork:{value}" for value in sorted(shared_fieldwork)
            )
        left_group = str(left_lineage.get("evidence_base_group_id") or "")
        right_group = str(right_lineage.get("evidence_base_group_id") or "")
        if (
            left_group
            and left_group == right_group
            and bool(left_lineage.get("counted_as_independent"))
            and bool(right_lineage.get("counted_as_independent"))
        ):
            reasons.append(f"declared_group:{left_group}")
        left_role = str(left.get("source_role") or "").casefold()
        right_role = str(right.get("source_role") or "").casefold()
        if any(
            marker in left_role
            for marker in ("practitioner", "guidance", "institutional")
        ) and any(
            marker in right_role
            for marker in ("practitioner", "guidance", "institutional")
        ):
            left_institution = _canonical_phrase(left_lineage.get("institution"))
            right_institution = _canonical_phrase(right_lineage.get("institution"))
            if left_institution and left_institution == right_institution:
                reasons.append(f"institutional_guidance:{left_institution}")
        if reasons:
            union(left_index, right_index, reasons)

    members_by_root: dict[int, list[int]] = defaultdict(list)
    for index in range(len(rows)):
        members_by_root[find(index)].append(index)
    for root, member_indexes in sorted(members_by_root.items()):
        if len(member_indexes) == 1:
            continue
        reasons = sorted(reasons_by_root.get(find(root), set()))
        group_id = f"evidence-base-{_stable_hash(reasons or [rows[index]['source_id'] for index in member_indexes])[:16]}"
        for index in member_indexes:
            row = rows[index]
            row["evidence_base_group_id"] = group_id
            lineage = _as_mapping(row.get("study_lineage"))
            lineage.update(
                evidence_base_group_id=group_id,
                group_basis=";".join(reasons),
                independence_status=(
                    "institutional_series"
                    if any(
                        reason.startswith("institutional_guidance:")
                        for reason in reasons
                    )
                    else "overlapping_evidence_base"
                ),
                counted_as_independent=True,
            )
            row["study_lineage"] = lineage
            for claim in row.get("claims", []) or []:
                claim["evidence_base_group_id"] = group_id
                claim["evidence_base_counted"] = True


def normalize_evidence_profiles(profiles: Sequence[Any]) -> list[dict[str, Any]]:
    """Pure compatibility boundary for EvidenceProfile models and current note rows."""
    normalized: list[dict[str, Any]] = []
    for position, value in enumerate(profiles):
        raw = _as_mapping(value)
        context = raw.get("context") if isinstance(raw.get("context"), Mapping) else {}
        context_metadata = (
            context.get("metadata")
            if isinstance(context.get("metadata"), Mapping)
            else {}
        )
        features = (
            raw.get("features") if isinstance(raw.get("features"), Mapping) else {}
        )
        title = str(
            raw.get("title")
            or context.get("title")
            or context_metadata.get("title")
            or ""
        ).strip()
        source_id = str(
            raw.get("source_id")
            or raw.get("id")
            or f"source-{_stable_hash([title, position])[:12]}"
        )
        note_id = str(raw.get("note_id") or f"note-{_stable_hash(source_id)[:12]}")
        doi = str(raw.get("doi") or raw.get("DOI") or "").strip().casefold()
        family_id = str(
            raw.get("study_family_id")
            or raw.get("study_id")
            or (f"doi:{doi}" if doi else source_id)
        )
        coverage = (
            raw.get("coverage") if isinstance(raw.get("coverage"), Mapping) else {}
        )
        status = str(
            raw.get("note_status")
            or raw.get("profile_status")
            or raw.get("status")
            or coverage.get("note_status")
            or context.get("note_status")
            or "analytical"
        ).casefold()
        coverage_status = str(coverage.get("status", "")).casefold()
        validity_status = str(
            (raw.get("validity") or {}).get("status", "")
            if isinstance(raw.get("validity"), Mapping)
            else ""
        ).casefold()
        excluded = bool(raw.get("excluded_from_synthesis", False))
        invalid = validity_status in {"invalid", "failed", "excluded"}
        limited_coverage = coverage_status in {
            "abstract",
            "abstract_only",
            "metadata_only",
            "limited",
            "failed",
        }
        analytical = (
            bool(raw.get("analytical", status in ANALYTICAL_STATUSES))
            and status not in LIMITED_STATUSES
            and not excluded
            and not invalid
            and not limited_coverage
        )
        claims = _normalize_claims(raw, source_id, family_id)
        metadata_date = str(
            raw.get("date")
            or context.get("date")
            or context_metadata.get("date")
            or ""
        )
        publication_year_match = re.search(r"\b(19\d{2}|20\d{2})\b", metadata_date)
        publication_year = (
            int(publication_year_match.group(1)) if publication_year_match else None
        )
        future_observed_years: set[int] = set()
        if publication_year is not None:
            for claim in claims:
                claim_text = " ".join(
                    str(value or "")
                    for value in (
                        claim.get("text"),
                        claim.get("magnitude"),
                        claim.get("comparison"),
                        claim.get("conditions"),
                        _as_mapping(claim.get("support_envelope")).get("scope"),
                    )
                )
                if not re.search(
                    r"\b(?:forecast|project(?:s|ed|ion)?|scenario|target|by design)\b",
                    claim_text,
                    flags=re.I,
                ):
                    future_observed_years.update(
                        int(year)
                        for year in re.findall(r"\b(19\d{2}|20\d{2})\b", claim_text)
                        if int(year) > publication_year + 1
                    )
        identity_conflict = len(future_observed_years) >= 2
        bibliographic_identity_status = (
            "source_identity_conflict" if identity_conflict else "not_flagged"
        )
        identity_explanation = ""
        if identity_conflict:
            observed_range = (
                str(min(future_observed_years))
                if len(future_observed_years) == 1
                else f"{min(future_observed_years)}-{max(future_observed_years)}"
            )
            identity_explanation = (
                f"Metadata dates this item to {publication_year}, but the captured "
                f"evidence reports observed facts from {observed_range}. The attachment "
                "or parent Zotero record must be reconciled before analytical synthesis."
            )
            analytical = False
        study_lineage = _normalized_study_lineage(
            raw,
            source_id=source_id,
            family_id=family_id,
            doi=doi,
        )
        for claim in claims:
            claim["evidence_base_group_id"] = study_lineage["evidence_base_group_id"]
            claim["evidence_base_counted"] = bool(
                study_lineage.get("counted_as_independent")
            )
            claim["independence_status"] = str(
                study_lineage.get("independence_status") or ""
            )
        dimensions = {
            dimension: _dimension_values(raw, dimension)
            for dimension in EVIDENCE_DIMENSIONS
        }
        normalized_tag_values = _flatten_values(
            raw.get("normalized_tags")
            or context_metadata.get("normalized_tags")
            or context.get("normalized_tags")
            or features.get("zotero_tag_context")
        )
        normalized_tags = sorted(
            {slugify(tag) for tag in normalized_tag_values if slugify(tag)}
        )
        tag_values = _flatten_values(
            raw.get("normalized_tags")
            or raw.get("tags")
            or context_metadata.get("normalized_tags")
            or context.get("normalized_tags")
            or features.get("zotero_tag_context")
        )
        tags = sorted(
            {_canonical_phrase(tag) for tag in tag_values if _canonical_phrase(tag)}
        )
        semantic_raw = dict(raw)
        semantic_raw["title"] = title
        if tags:
            tag_set = set(tags)
            for field in _SEMANTIC_FIELDS:
                semantic_raw[field] = [
                    value
                    for value in _flatten_values(raw.get(field))
                    if _canonical_phrase(value) not in tag_set
                ]
            semantic_claims = []
            for claim in claims:
                cleaned_claim = dict(claim)
                if _canonical_phrase(cleaned_claim.get("topic")) in tag_set:
                    cleaned_claim["topic"] = ""
                cleaned_dimensions = {
                    dimension: [
                        value
                        for value in values
                        if _canonical_phrase(value) not in tag_set
                    ]
                    for dimension, values in cleaned_claim.get("dimensions", {}).items()
                }
                cleaned_claim["dimensions"] = cleaned_dimensions
                semantic_claims.append(cleaned_claim)
        else:
            semantic_claims = claims
        topic_scores = _topic_scores(semantic_raw, semantic_claims)
        topic_labels = _topic_labels(semantic_raw, semantic_claims, topic_scores)
        author_gap_records = [
            record
            for origin, values in (
                (
                    "author_stated_gap",
                    raw.get("author_stated_gaps") or raw.get("gaps") or [],
                ),
                ("future_research", raw.get("future_research") or []),
            )
            for value in values
            if (record := _author_gap_record(value, origin=origin)) is not None
        ]
        search_values = [
            title,
            *topic_scores,
            *[claim.get("text", "") for claim in claims],
        ]
        for dimension in EVIDENCE_DIMENSIONS:
            search_values.extend(dimensions[dimension])
        normalized.append(
            {
                "_normalized": True,
                "source_id": source_id,
                "note_id": note_id,
                "title": title,
                "study_family_id": family_id,
                "evidence_base_group_id": study_lineage["evidence_base_group_id"],
                "evidence_base_counted": bool(
                    study_lineage.get("counted_as_independent")
                ),
                "study_lineage": study_lineage,
                "source_role": str(raw.get("source_role") or "analytical_source"),
                "research_questions": list(raw.get("research_questions") or []),
                "zotero_item_key": str(
                    raw.get("zotero_item_key") or context.get("zotero_item_key") or ""
                ),
                "note_status": status,
                "analytical": analytical,
                "limited": not analytical,
                "exclusion_reason": (
                    identity_explanation
                    or str(
                        raw.get("exclusion_reason")
                        or context.get("exclusion_reason")
                        or ""
                    )
                ),
                "bibliographic_identity_status": bibliographic_identity_status,
                "bibliographic_identity_explanation": identity_explanation,
                "semantic_topic_scores": topic_scores,
                "semantic_topic_labels": topic_labels,
                "dimensions": dimensions,
                "claims": claims,
                "evidence_anchors": claims,
                "quantitative_results": [
                    _as_mapping(row)
                    for row in raw.get("quantitative_results", []) or []
                    if isinstance(row, Mapping)
                ],
                "source_locators": [
                    _as_mapping(row)
                    for row in raw.get("source_locators", []) or []
                    if isinstance(row, Mapping)
                ],
                "tags": tags,
                "normalized_tags": normalized_tags,
                "relations": raw.get("citation_relations")
                or raw.get("relations")
                or raw.get("zotero_relations")
                or context.get("zotero_relations")
                or {},
                "gap_signals": list(
                    raw.get("gap_signals") or raw.get("gap_candidates") or []
                ),
                "author_stated_gaps": author_gap_records,
                "gap_answers": list(
                    raw.get("gap_answers") or raw.get("answered_gaps") or []
                ),
                "note_path": str(
                    raw.get("note_path") or context.get("note_path") or ""
                ),
                "note_hash": str(raw.get("note_hash") or ""),
                "date": str(raw.get("date") or context.get("date") or ""),
                "search_tokens": sorted(_tokens(search_values)),
            }
        )
    _reconcile_evidence_base_groups(normalized)
    return sorted(normalized, key=lambda row: (row["source_id"], row["note_id"]))


def _ensure_profiles(profiles: Sequence[Any]) -> list[dict[str, Any]]:
    if all(isinstance(row, Mapping) and row.get("_normalized") for row in profiles):
        return [
            dict(row) for row in profiles
        ]  # defensive copies keep stages pure for callers
    return normalize_evidence_profiles(profiles)


def _relation_strings(value: Any) -> list[str]:
    if isinstance(value, Mapping):
        rows: list[str] = []
        for key, item in value.items():
            rows.append(str(key))
            rows.extend(_relation_strings(item))
        return rows
    return _flatten_values(value)


def _has_explicit_relation(
    source: Mapping[str, Any], target: Mapping[str, Any]
) -> bool:
    haystack = " ".join(_relation_strings(source.get("relations"))).casefold()
    aliases = {
        str(target.get("source_id", "")).casefold(),
        str(target.get("note_id", "")).casefold(),
        str(target.get("zotero_item_key", "")).casefold(),
    } - {""}
    return any(alias in haystack for alias in aliases)


def map_profile_relations(profiles: Sequence[Any]) -> list[dict[str, Any]]:
    """Map evidence relations; tag overlap is recorded only after a substantive relation exists."""
    rows = [row for row in _ensure_profiles(profiles) if row["analytical"]]
    relations: list[dict[str, Any]] = []
    for left, right in combinations(rows, 2):
        shared_topics = sorted(
            set(left["semantic_topic_scores"]) & set(right["semantic_topic_scores"])
        )
        left_findings = (
            set().union(*(_tokens(claim.get("text", "")) for claim in left["claims"]))
            if left["claims"]
            else set()
        )
        right_findings = (
            set().union(*(_tokens(claim.get("text", "")) for claim in right["claims"]))
            if right["claims"]
            else set()
        )
        shared_findings = sorted(left_findings & right_findings)
        finding_overlap = len(shared_findings) / max(
            1, min(len(left_findings), len(right_findings))
        )
        structured_finding_match = len(shared_findings) >= 2 and finding_overlap >= 0.4
        explicit_lr = _has_explicit_relation(left, right)
        explicit_rl = _has_explicit_relation(right, left)
        if not (
            shared_topics or structured_finding_match or explicit_lr or explicit_rl
        ):
            continue
        evidence: list[dict[str, Any]] = []
        if shared_topics:
            evidence.append({"kind": "semantic_profile", "values": shared_topics})
        if structured_finding_match:
            evidence.append(
                {
                    "kind": "structured_findings",
                    "values": shared_findings,
                    "overlap_coefficient": round(finding_overlap, 3),
                }
            )
        if explicit_lr or explicit_rl:
            evidence.append(
                {
                    "kind": "explicit_zotero_or_citation_relation",
                    "directions": [
                        direction
                        for flag, direction in (
                            (explicit_lr, "left_to_right"),
                            (explicit_rl, "right_to_left"),
                        )
                        if flag
                    ],
                }
            )
        shared_tags = sorted(set(left["tags"]) & set(right["tags"]))
        if shared_tags:
            evidence.append(
                {"kind": "tag_tiebreaker", "values": shared_tags, "weight": "weak"}
            )
        confidence = min(
            0.99,
            0.45
            + 0.12 * len(shared_topics)
            + (0.04 * min(len(shared_findings), 4) if structured_finding_match else 0)
            + (0.25 if explicit_lr or explicit_rl else 0),
        )
        source_ids = sorted((left["source_id"], right["source_id"]))
        relations.append(
            {
                "relation_id": f"relation-{_stable_hash(source_ids)[:12]}",
                "source_ids": source_ids,
                "confidence": round(confidence, 3),
                "evidence": evidence,
            }
        )
    return sorted(relations, key=lambda row: row["relation_id"])


def map_topic_neighborhoods(
    profiles: Sequence[Any],
    relations: Sequence[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build unlimited navigation neighborhoods without analytical force.

    Neighborhoods expose broad topic, case, method, citation, and tag
    relationships for retrieval and the Obsidian graph. They never satisfy a
    cluster, debate, or gap threshold.
    """

    rows = _ensure_profiles(profiles)
    by_identity: dict[tuple[str, str], dict[str, Any]] = {}

    def add(kind: str, label: str, profile: Mapping[str, Any], strength: str) -> None:
        identity = _canonical_phrase(label)
        if not identity or set(identity.split()).issubset(_GENERIC_TOPIC_IDENTITIES):
            return
        key = (kind, identity)
        neighborhood = by_identity.setdefault(
            key,
            {
                "topic_neighborhood_id": f"neighborhood-{slugify(kind)}-{_stable_hash([kind, identity])[:12]}",
                "kind": kind,
                "semantic_identity": identity,
                "label": re.sub(r"\s+", " ", str(label)).strip(),
                "source_ids": [],
                "note_ids": [],
                "signals": [],
                "analytical_support": False,
            },
        )
        source_id = str(profile.get("source_id") or "")
        note_id = str(profile.get("note_id") or "")
        if source_id and source_id not in neighborhood["source_ids"]:
            neighborhood["source_ids"].append(source_id)
        if note_id and note_id not in neighborhood["note_ids"]:
            neighborhood["note_ids"].append(note_id)
        signal = {"source_id": source_id, "kind": kind, "strength": strength}
        if signal not in neighborhood["signals"]:
            neighborhood["signals"].append(signal)

    for profile in rows:
        for identity, score in profile.get("semantic_topic_scores", {}).items():
            add(
                "semantic",
                str(profile.get("semantic_topic_labels", {}).get(identity) or identity),
                profile,
                "strong" if float(score) >= 0.8 else "moderate",
            )
        for value in profile.get("dimensions", {}).get("case", []) or []:
            add("case", str(value), profile, "moderate")
        for value in profile.get("dimensions", {}).get("method", []) or []:
            add("method", str(value), profile, "moderate")
        for value in profile.get("normalized_tags", []) or []:
            add("tag", str(value), profile, "weak")

    profile_by_source = {str(row.get("source_id") or ""): row for row in rows}
    for relation in relations or []:
        relation_sources = [
            profile_by_source[str(source_id)]
            for source_id in relation.get("source_ids", []) or []
            if str(source_id) in profile_by_source
        ]
        if len(relation_sources) != 2:
            continue
        label = " / ".join(
            str(row.get("title") or row.get("source_id") or "")
            for row in relation_sources
        )
        for profile in relation_sources:
            add("citation_or_relation", label, profile, "strong")

    for neighborhood in by_identity.values():
        neighborhood["source_ids"] = sorted(set(neighborhood["source_ids"]))
        neighborhood["note_ids"] = sorted(set(neighborhood["note_ids"]))
        neighborhood["signals"] = sorted(
            neighborhood["signals"],
            key=lambda row: (row["source_id"], row["kind"], row["strength"]),
        )
        neighborhood["source_count"] = len(neighborhood["source_ids"])
    return sorted(by_identity.values(), key=lambda row: row["topic_neighborhood_id"])


def _anchor_scope_values(anchor: Mapping[str, Any], key: str) -> list[str]:
    envelope = _as_mapping(anchor.get("support_envelope"))
    scope = _as_mapping(envelope.get("scope"))
    scoped = scope.get(key) or scope.get(f"{key}s") or []
    values = _flatten_values(scoped)
    if not values:
        values = list(_as_mapping(anchor.get("dimensions")).get(key, []) or [])
    return [str(value) for value in values if str(value).strip()]


def _anchor_is_synthesis_eligible(anchor: Mapping[str, Any]) -> bool:
    envelope = _as_mapping(anchor.get("support_envelope"))
    return bool(
        anchor.get("locator_complete")
        and str(envelope.get("coverage") or "unknown") == "full_text"
        and str(
            envelope.get("support_status")
            or anchor.get("support_status")
            or "support_unknown"
        )
        == "supported"
        and (
            str(envelope.get("empirical_role") or "none") in EMPIRICAL_SUPPORT_ROLES
            or str(envelope.get("argument_role") or "none") in ARGUMENT_SUPPORT_ROLES
        )
    )


def _proposition_signature(anchor: Mapping[str, Any]) -> dict[str, Any]:
    topic, outcome, relationship = _claim_proposition_parts(anchor)
    if (
        not relationship
        and topic
        and str(anchor.get("direction") or "not_reported") != "not_reported"
    ):
        # Compatibility rows sometimes state only that a topic has a reported
        # directional result. That is weak but still an asserted relationship,
        # unlike tags or source-level topic metadata alone.
        relationship = {"reported_relationship"}
    envelope = _as_mapping(anchor.get("support_envelope"))
    empirical_role = str(envelope.get("empirical_role") or "none")
    argument_role = str(envelope.get("argument_role") or "none")
    evidence_family = "empirical" if empirical_role != "none" else argument_role
    return {
        "topic": sorted(topic),
        "outcome": sorted(outcome),
        "relationship": sorted(relationship),
        "evidence_family": evidence_family,
    }


def _proposition_statement(
    anchors: Sequence[Mapping[str, Any]], signature: Mapping[str, Any]
) -> str:
    texts = [
        str(row.get("text") or "").strip()
        for row in anchors
        if str(row.get("text") or "").strip()
    ]
    if texts:
        return sorted(texts, key=lambda value: (len(value), value.casefold()))[0]
    terms = [
        *[str(value) for value in signature.get("topic", []) or []],
        *[str(value) for value in signature.get("relationship", []) or []],
        *[str(value) for value in signature.get("outcome", []) or []],
    ]
    return " ".join(dict.fromkeys(terms)).strip()


def build_literature_propositions(profiles: Sequence[Any]) -> list[dict[str, Any]]:
    """Group located source anchors into comparable map-local propositions."""

    rows = _ensure_profiles(profiles)
    eligible = [
        anchor
        for profile in rows
        if profile.get("analytical")
        for anchor in profile.get("claims", []) or []
        if _anchor_is_synthesis_eligible(anchor)
    ]
    by_signature: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for anchor in eligible:
        signature = _proposition_signature(anchor)
        if not signature["relationship"] and not signature["outcome"]:
            # Shared subject vocabulary without an asserted relationship is a
            # topic neighborhood, not an analytical proposition.
            continue
        identity = _stable_hash(signature)
        by_signature[identity].append(dict(anchor))

    propositions: list[dict[str, Any]] = []
    for identity, anchors in sorted(by_signature.items()):
        sources = sorted({str(row.get("source_id") or "") for row in anchors})
        if len(sources) < 2:
            continue
        families = sorted(
            {
                str(row.get("study_family_id") or row.get("source_id") or "")
                for row in anchors
            }
        )
        evidence_bases = sorted(
            {
                evidence_base_id
                for row in anchors
                if (evidence_base_id := _anchor_evidence_base_id(row))
            }
        )
        signature = _proposition_signature(anchors[0])
        cells: list[dict[str, Any]] = []
        for source_id in sources:
            source_anchors = [
                row for row in anchors if str(row.get("source_id") or "") == source_id
            ]
            evidence = [_evidence_ref(row) for row in source_anchors]
            cells.append(
                {
                    "source_id": source_id,
                    "study_family_id": str(
                        source_anchors[0].get("study_family_id") or source_id
                    ),
                    "evidence_base_group_id": str(
                        source_anchors[0].get("evidence_base_group_id") or ""
                    ),
                    "counted_as_independent": bool(
                        source_anchors[0].get("evidence_base_counted")
                    ),
                    "stance_or_finding": "; ".join(
                        dict.fromkeys(
                            str(row.get("text") or "").strip()
                            for row in source_anchors
                            if row.get("text")
                        )
                    ),
                    "evidence_type": sorted(
                        {
                            str(row.get("evidence_role") or "support_unknown")
                            for row in source_anchors
                        }
                    ),
                    "scope": {
                        key: sorted(
                            {
                                value
                                for row in source_anchors
                                for value in _anchor_scope_values(row, key)
                            }
                        )
                        for key in (
                            "population",
                            "case",
                            "geography",
                            "period",
                            "outcome",
                        )
                    },
                    "boundary_conditions": sorted(
                        {
                            str(row.get("boundary_condition") or "")
                            for row in source_anchors
                            if row.get("boundary_condition")
                        }
                    ),
                    "direction_or_interpretation": sorted(
                        {
                            str(row.get("direction") or "not_reported")
                            for row in source_anchors
                        }
                    ),
                    "uncertainty": sorted(
                        {
                            str(row.get("uncertainty") or "")
                            for row in source_anchors
                            if row.get("uncertainty")
                        }
                    ),
                    "evidence": evidence,
                }
            )
        proposition_id = f"proposition-{_stable_hash(signature)[:16]}"
        propositions.append(
            {
                "proposition_id": proposition_id,
                "semantic_identity": identity,
                "statement": _proposition_statement(anchors, signature),
                "question": f"What does the collection establish about {_proposition_statement(anchors, signature)}?",
                "proposition_type": str(signature.get("evidence_family") or "unknown"),
                "signature": signature,
                "source_ids": sources,
                "study_family_ids": families,
                "evidence_base_group_ids": evidence_bases,
                "independent_study_family_count": len(families),
                "effective_evidence_base_count": len(evidence_bases),
                "publication_count": len(sources),
                "cells": cells,
                "evidence": [_evidence_ref(row) for row in anchors],
                "comparability": {
                    "passed": True,
                    "basis": "shared_source_local_relationship_signature",
                    "independence_passed": len(evidence_bases) >= 2,
                    "outcomes": list(signature.get("outcome", []) or []),
                    "evidence_family": str(
                        signature.get("evidence_family") or "unknown"
                    ),
                },
            }
        )
    return sorted(propositions, key=lambda row: row["proposition_id"])


def _profile_evidence_base_id(profile: Mapping[str, Any]) -> str:
    lineage = _as_mapping(profile.get("study_lineage"))
    counted = profile.get("evidence_base_counted")
    if counted is None:
        counted = lineage.get("counted_as_independent")
    if counted is not True:
        return ""
    return str(
        profile.get("evidence_base_group_id")
        or lineage.get("evidence_base_group_id")
        or ""
    )


def _anchor_evidence_base_id(anchor: Mapping[str, Any]) -> str:
    if anchor.get("evidence_base_counted") is not True:
        return ""
    return str(anchor.get("evidence_base_group_id") or "")


def _reference_evidence_base_id(reference: Mapping[str, Any]) -> str:
    if reference.get("counted_as_independent") is not True:
        return ""
    return str(reference.get("evidence_base_group_id") or "")


def _map_topic_clusters_legacy(
    profiles: Sequence[Any],
    relations: Sequence[Mapping[str, Any]] | None = None,
    *,
    policy: Any = None,
    proposals: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build deterministic semantic clusters with at most three memberships per source."""
    rows = _ensure_profiles(profiles)
    analytical = [row for row in rows if row["analytical"]]
    max_memberships = max(
        1,
        min(
            3,
            int(
                _policy_value(
                    policy,
                    (
                        "max_memberships",
                        "max_cluster_memberships",
                        "max_overlapping_clusters",
                    ),
                    3,
                )
            ),
        ),
    )
    min_emerging = max(
        2,
        int(
            _policy_value(
                policy, ("min_emerging_families", "emerging_cluster_min_sources"), 2
            )
        ),
    )
    min_backed = max(
        3,
        int(
            _policy_value(
                policy,
                (
                    "source_backed_threshold",
                    "min_source_backed_families",
                    "source_backed_cluster_min_sources",
                ),
                3,
            )
        ),
    )
    auto_promote = bool(_policy_value(policy, "auto_promote_clusters", True))
    by_topic: dict[str, list[dict[str, Any]]] = defaultdict(list)
    profile_by_source = {row["source_id"]: row for row in analytical}
    reasoned_metadata: dict[str, dict[str, Any]] = {}
    reasoner_clustered_sources: set[str] = set()
    proposal_rejections: list[dict[str, Any]] = []
    for raw_proposal in proposals or ():
        proposal = _as_mapping(raw_proposal)
        identity = _canonical_phrase(
            proposal.get("semantic_identity") or proposal.get("label")
        )
        source_ids = sorted(
            {str(value) for value in proposal.get("source_ids", []) or [] if str(value)}
        )
        evidence = [
            _as_mapping(value)
            for value in proposal.get("supporting_evidence", []) or []
            if isinstance(value, Mapping) or is_dataclass(value)
        ]
        valid_evidence: list[dict[str, Any]] = []
        valid_sources: set[str] = set()
        for source_id in source_ids:
            profile = profile_by_source.get(source_id)
            if profile is None:
                continue
            supplied = [
                reference
                for reference in evidence
                if str(reference.get("source_id") or "") == source_id
            ]
            if supplied:
                matched = [
                    reference
                    for reference in supplied
                    if _reference_matches_profile(reference, profile)
                ]
                if not matched:
                    # An explicitly invented or paraphrased reference is never
                    # repaired silently.
                    continue
                reference = dict(matched[0])
            else:
                reference = _proposal_membership_evidence(profile, proposal)
                if not reference:
                    continue
            valid_sources.add(source_id)
            valid_evidence.append(reference)
        families = {
            profile_by_source[source_id]["study_family_id"]
            for source_id in valid_sources
        }
        if not identity or not source_ids or len(families) < min_emerging:
            proposal_rejections.append(
                {
                    "semantic_identity": identity or str(proposal.get("label") or ""),
                    "source_ids": source_ids,
                    "action": "reject",
                    "reason": "reasoner_proposal_missing_independent_locator_backed_membership",
                    "proposal_id": str(proposal.get("proposal_id") or ""),
                }
            )
            continue
        dropped_sources = sorted(set(source_ids) - valid_sources)
        if dropped_sources:
            proposal_rejections.append(
                {
                    "semantic_identity": identity,
                    "source_ids": dropped_sources,
                    "action": "narrow",
                    "reason": "reasoner_proposal_membership_missing_exact_claim_locator",
                    "proposal_id": str(proposal.get("proposal_id") or ""),
                }
            )
        reasoned_metadata[identity] = {
            "proposal_id": str(proposal.get("proposal_id") or ""),
            "label": str(
                proposal.get("label") or proposal.get("semantic_identity") or identity
            ),
            "shared_question": str(proposal.get("shared_question") or ""),
            "coherence_rationale": str(proposal.get("coherence_rationale") or ""),
            "supporting_evidence": valid_evidence,
        }
        reasoner_clustered_sources.update(valid_sources)
        for source_id in sorted(valid_sources):
            by_topic[identity].append(
                {"profile": profile_by_source[source_id], "score": 1.0}
            )

    # Valid reasoner proposals enrich the map; they never suppress coherent
    # deterministic clusters when the proposal packet is partial.
    for profile in analytical:
        if profile["source_id"] in reasoner_clustered_sources:
            continue
        for identity, score in profile["semantic_topic_scores"].items():
            by_topic[identity].append({"profile": profile, "score": score})
    for relation in relations or ():
        relation_profiles = [
            profile_by_source[str(source_id)]
            for source_id in relation.get("source_ids", []) or []
            if str(source_id) in profile_by_source
        ]
        if len(relation_profiles) < 2:
            continue
        shared_topics = set(relation_profiles[0]["semantic_topic_scores"])
        for profile in relation_profiles[1:]:
            shared_topics &= set(profile["semantic_topic_scores"])
        if shared_topics:
            continue
        structured_values = sorted(
            {
                str(value)
                for evidence in relation.get("evidence", []) or []
                if evidence.get("kind") == "structured_findings"
                for value in evidence.get("values", []) or []
            }
        )
        relation_identity = _canonical_phrase(structured_values)
        if len(relation_identity.split()) < 2:
            continue
        for profile in relation_profiles:
            by_topic[relation_identity].append({"profile": profile, "score": 0.6})

    candidates: dict[str, dict[str, Any]] = {}
    rejected: list[dict[str, Any]] = list(proposal_rejections)
    for identity, members in sorted(by_topic.items()):
        unique = {row["profile"]["source_id"]: row for row in members}
        families = {row["profile"]["study_family_id"] for row in unique.values()}
        if len(families) < min_emerging:
            rejected.append(
                {
                    "semantic_identity": identity,
                    "source_ids": sorted(unique),
                    "action": "reject",
                    "reason": "singleton_cluster"
                    if len(unique) == 1
                    else "insufficient_independent_study_families",
                }
            )
            continue
        candidates[identity] = {
            "identity": identity,
            "members": unique,
            "family_count": len(families),
            "mean_score": sum(float(row["score"]) for row in unique.values())
            / len(unique),
        }

    kept_candidates: dict[str, dict[str, Any]] = {}
    ordered_candidates = sorted(
        candidates.values(),
        key=lambda row: (
            -len(row["members"]),
            len(str(row["identity"]).split()),
            str(row["identity"]),
        ),
    )
    for candidate in ordered_candidates:
        candidate_tokens = set(str(candidate["identity"]).split())
        candidate_sources = set(candidate["members"])
        superseded_by = ""
        for kept in kept_candidates.values():
            kept_tokens = set(str(kept["identity"]).split())
            if not (candidate_tokens <= kept_tokens or kept_tokens <= candidate_tokens):
                continue
            kept_sources = set(kept["members"])
            membership_jaccard = len(candidate_sources & kept_sources) / max(
                1, len(candidate_sources | kept_sources)
            )
            if membership_jaccard >= 0.8:
                superseded_by = str(kept["identity"])
                break
        if superseded_by:
            rejected.append(
                {
                    "semantic_identity": candidate["identity"],
                    "source_ids": sorted(candidate_sources),
                    "action": "reject",
                    "reason": "redundant_semantic_cluster",
                    "superseded_by": superseded_by,
                }
            )
        else:
            kept_candidates[str(candidate["identity"])] = candidate
    candidates = kept_candidates

    selected_by_source: dict[str, set[str]] = defaultdict(set)
    for profile in analytical:
        available = [
            candidate
            for candidate in candidates.values()
            if profile["source_id"] in candidate["members"]
        ]
        available.sort(
            key=lambda row: (-row["family_count"], -row["mean_score"], row["identity"])
        )
        selected_by_source[profile["source_id"]].update(
            row["identity"] for row in available[:max_memberships]
        )

    relation_ids_by_source: dict[str, list[str]] = defaultdict(list)
    for relation in relations or ():
        for source_id in relation.get("source_ids", []) or []:
            relation_ids_by_source[str(source_id)].append(
                str(relation.get("relation_id", ""))
            )

    clusters: list[dict[str, Any]] = []
    for identity, candidate in sorted(candidates.items()):
        member_rows = [
            row["profile"]
            for source_id, row in candidate["members"].items()
            if identity in selected_by_source[source_id]
        ]
        families = sorted({row["study_family_id"] for row in member_rows})
        if len(families) < min_emerging:
            rejected.append(
                {
                    "semantic_identity": identity,
                    "source_ids": sorted(row["source_id"] for row in member_rows),
                    "action": "reject",
                    "reason": "overlap_policy_removed_coherence",
                }
            )
            continue
        source_ids = sorted(row["source_id"] for row in member_rows)
        note_ids = sorted(row["note_id"] for row in member_rows)
        reasoned = reasoned_metadata.get(identity, {})
        label_counts = Counter(
            str(row.get("semantic_topic_labels", {}).get(identity) or identity)
            for row in member_rows
        )
        label = str(
            reasoned.get("label")
            or sorted(
                label_counts,
                key=lambda value: (-label_counts[value], len(value), value.casefold()),
            )[0]
        )
        cluster_id = f"cluster-{slugify(identity)}-{_stable_hash({'semantic_identity': identity})[:10]}"
        revision_hash = _stable_hash(
            {
                "cluster_id": cluster_id,
                "source_ids": source_ids,
                "study_family_ids": families,
            }
        )
        qualification_status = (
            "source_backed_cluster"
            if len(families) >= min_backed
            else "emerging_cluster"
        )
        tag_families: dict[str, set[str]] = defaultdict(set)
        for row in member_rows:
            for tag in row.get("normalized_tags", []) or []:
                tag_families[str(tag)].add(str(row["study_family_id"]))
        shared_normalized_tags = sorted(
            tag
            for tag, tag_study_families in tag_families.items()
            if len(tag_study_families) >= min_emerging
        )
        clusters.append(
            {
                "cluster_id": cluster_id,
                "semantic_identity": identity,
                "label": label,
                "shared_question": str(
                    reasoned.get("shared_question")
                    or f"What does the mapped evidence establish about {label}?"
                ),
                "coherence_rationale": str(
                    reasoned.get("coherence_rationale")
                    or f"Independent profiles share the mapped semantic identity: {label}."
                ),
                "proposal_id": str(reasoned.get("proposal_id") or ""),
                "proposal_supporting_evidence": list(
                    reasoned.get("supporting_evidence", []) or []
                ),
                "formation_route": "reasoner_proposal"
                if reasoned
                else "deterministic_fallback",
                "shared_concepts": [identity],
                "shared_normalized_tags": shared_normalized_tags,
                "shared_methods": sorted(
                    {
                        value
                        for row in member_rows
                        for value in row["dimensions"]["method"]
                    }
                ),
                "note_ids": note_ids,
                "source_ids": source_ids,
                "study_family_ids": families,
                "independent_study_family_count": len(families),
                "source_count": len(source_ids),
                "status": qualification_status if auto_promote else "cluster_candidate",
                "qualification_status": qualification_status,
                "promoted": auto_promote,
                "automation_status": "promoted" if auto_promote else "candidate",
                "source_backed": len(families) >= min_backed,
                "revision_hash": revision_hash,
                "relation_ids": sorted(
                    {
                        relation_id
                        for source_id in source_ids
                        for relation_id in relation_ids_by_source[source_id]
                        if relation_id
                    }
                ),
                "representative_sources": [
                    {
                        "note_id": row["note_id"],
                        "source_id": row["source_id"],
                        "study_family_id": row["study_family_id"],
                        "title": row["title"],
                        "note_path": row["note_path"],
                        "note_hash": row["note_hash"],
                    }
                    for row in sorted(member_rows, key=lambda item: item["source_id"])
                ],
            }
        )

    clustered_sources = {
        source_id for cluster in clusters for source_id in cluster["source_ids"]
    }
    unclustered = []
    for profile in rows:
        if profile["source_id"] in clustered_sources:
            continue
        if profile["limited"]:
            reason = (
                profile.get("exclusion_reason")
                or "limited_profile_excluded_from_analytical_clustering"
            )
        elif not profile["semantic_topic_scores"]:
            reason = "no_semantic_topic_identity"
        else:
            reason = "no_coherent_multi_family_cluster"
        unclustered.append(
            {
                "source_id": profile["source_id"],
                "note_id": profile["note_id"],
                "reason": reason,
            }
        )
    return {
        "clusters": sorted(clusters, key=lambda row: row["cluster_id"]),
        "rejected_proposals": sorted(
            rejected, key=lambda row: (row["reason"], row["semantic_identity"])
        ),
        "unclustered_sources": sorted(unclustered, key=lambda row: row["source_id"]),
        "max_cluster_memberships": max_memberships,
    }


def _proposal_source_roles(proposal: Mapping[str, Any]) -> dict[str, str]:
    supplied = proposal.get("source_roles")
    result: dict[str, str] = {}
    if isinstance(supplied, Mapping):
        result = {
            str(source_id): str(role).casefold() for source_id, role in supplied.items()
        }
    elif isinstance(supplied, Sequence) and not isinstance(
        supplied, (str, bytes, bytearray)
    ):
        for row in supplied:
            if not isinstance(row, Mapping):
                continue
            source_id = str(row.get("source_id") or "")
            role = str(row.get("role") or "").casefold()
            if source_id:
                result[source_id] = role
    return {
        source_id: role
        for source_id, role in result.items()
        if role in {"core", "context", "bridge"}
    }


def _family_relation_semantic_assessment(
    proposal: Mapping[str, Any],
    relation_type: str,
    source_ids: Sequence[str],
    evidence: Sequence[Mapping[str, Any]],
    profile_by_source: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Verify that a typed edge joins one evidenced object, not merely two named sources."""

    family_terms = (
        _tokens(
            [
                proposal.get("bounded_object"),
                proposal.get("shared_question"),
                proposal.get("semantic_identity"),
                proposal.get("label"),
            ]
        )
        - _GENERIC_FAMILY_RELATION_TERMS
    )
    references_by_source: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for reference in evidence:
        references_by_source[str(reference.get("source_id") or "")].append(reference)

    structured_anchor_terms_by_source: dict[str, set[str]] = {}
    anchors_by_source: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for source_id in source_ids:
        profile = profile_by_source[source_id]
        anchor_by_id = {
            str(
                anchor.get("evidence_anchor_id") or anchor.get("claim_id") or ""
            ): anchor
            for anchor in profile.get("claims", []) or []
        }
        anchors = [
            anchor_by_id[anchor_id]
            for reference in references_by_source.get(source_id, [])
            if (
                anchor_id := str(
                    reference.get("evidence_anchor_id")
                    or reference.get("claim_id")
                    or reference.get("finding_id")
                    or ""
                )
            )
            in anchor_by_id
        ]
        anchors_by_source[source_id] = anchors
        structured_anchor_terms_by_source[source_id] = (
            _tokens(
                [
                    [
                        anchor.get("topic"),
                        *(
                            _as_mapping(anchor.get("dimensions")).get(dimension, [])
                            for dimension in (
                                "concept",
                                "theory",
                                "mechanism",
                                "outcome",
                                "case",
                                "population",
                                "geography",
                            )
                        ),
                        *(
                            _as_mapping(
                                _as_mapping(anchor.get("support_envelope")).get("scope")
                            ).get(dimension, [])
                            for dimension in (
                                "outcome",
                                "case",
                                "population",
                                "geography",
                            )
                        ),
                    ]
                    for anchor in anchors
                ]
            )
            - _GENERIC_FAMILY_RELATION_TERMS
        )
    shared_object_terms = (
        set.intersection(
            *(structured_anchor_terms_by_source[source_id] for source_id in source_ids)
        )
        if source_ids
        else set()
    ) & family_terms
    discriminating_shared_terms = shared_object_terms - _BROAD_FIELD_TERMS
    shared_object_passed = len(shared_object_terms) >= 2 and bool(
        discriminating_shared_terms
    )

    type_specific_passed = True
    type_specific_explanation = (
        "The relation type requires no additional deterministic dimension check."
    )
    if relation_type == "shared_research_problem":
        discriminating_family_terms = family_terms - _BROAD_FIELD_TERMS
        overlap_by_source: dict[str, list[str]] = {}
        for source_id, anchors in anchors_by_source.items():
            anchor_terms = (
                _tokens(
                    [
                        [
                            anchor.get("text"),
                            anchor.get("claim"),
                            anchor.get("plain_english_meaning"),
                            anchor.get("topic"),
                            anchor.get("dimensions"),
                        ]
                        for anchor in anchors
                    ]
                )
                - _GENERIC_FAMILY_RELATION_TERMS
            )
            overlap_by_source[source_id] = sorted(
                anchor_terms & discriminating_family_terms
            )
        shared_object_terms = set().union(
            *(set(values) for values in overlap_by_source.values())
        )
        shared_object_passed = bool(discriminating_family_terms) and all(
            overlap_by_source.values()
        )
        type_specific_passed = shared_object_passed
        type_specific_explanation = (
            "Every core source has a located anchor centrally addressing the bounded research problem."
            if shared_object_passed
            else "At least one core source lacks a located anchor addressing the bounded research problem."
        )
    elif relation_type == "same_proposition":
        exact_shared_subject_terms = (
            shared_object_terms
            - _BROAD_FIELD_TERMS
            - _OUTCOME_SIGNAL_TERMS
            - _NON_DISCRIMINATING_RELATIONSHIP_TERMS
        )
        type_specific_passed = bool(exact_shared_subject_terms)
        type_specific_explanation = (
            "The located anchors share an exact non-outcome subject term."
            if type_specific_passed
            else "The located anchors share only a broad field or outcome, not the same treatment, mechanism, actor, or interpretation."
        )
    elif relation_type in {"complementary_mechanism", "rival_explanation"}:
        mechanism_signatures_by_source = {
            source_id: {
                _canonical_phrase(value)
                for anchor in anchors
                for value in _flatten_values(
                    _as_mapping(anchor.get("dimensions")).get("mechanism", [])
                )
                if _canonical_phrase(value)
            }
            for source_id, anchors in anchors_by_source.items()
        }
        distinct_mechanisms = {
            value
            for values in mechanism_signatures_by_source.values()
            for value in values
        }
        mechanisms_link_to_shared_object = all(
            any(_tokens(mechanism) & shared_object_terms for mechanism in mechanisms)
            for mechanisms in mechanism_signatures_by_source.values()
        )
        type_specific_passed = (
            all(mechanism_signatures_by_source.values())
            and len(distinct_mechanisms) >= 2
            and mechanisms_link_to_shared_object
        )
        type_specific_explanation = (
            f"The located anchors identify mechanisms for {sum(bool(values) for values in mechanism_signatures_by_source.values())} "
            f"of {len(source_ids)} sources and {len(distinct_mechanisms)} distinct mechanism(s); "
            f"shared-object linkage is {'complete' if mechanisms_link_to_shared_object else 'incomplete'}."
        )
    elif relation_type == "sequential_relationship":
        outcome_signatures_by_source = {
            source_id: {
                _canonical_phrase(value)
                for anchor in anchors
                for value in _flatten_values(
                    [
                        _as_mapping(anchor.get("dimensions")).get("outcome", []),
                        _as_mapping(
                            _as_mapping(anchor.get("support_envelope")).get("scope")
                        ).get("outcome", []),
                    ]
                )
                if _canonical_phrase(value)
            }
            for source_id, anchors in anchors_by_source.items()
        }
        distinct_outcomes = {
            value
            for values in outcome_signatures_by_source.values()
            for value in values
        }
        type_specific_passed = (
            all(outcome_signatures_by_source.values()) and len(distinct_outcomes) >= 2
        )
        type_specific_explanation = (
            f"The located anchors identify outcomes or stages for {sum(bool(values) for values in outcome_signatures_by_source.values())} "
            f"of {len(source_ids)} sources and {len(distinct_outcomes)} distinct stage outcome(s)."
        )
    elif relation_type == "boundary_contrast":
        boundary_signatures = {
            _canonical_phrase(
                [
                    anchor.get("boundary_condition"),
                    _as_mapping(anchor.get("support_envelope")).get("scope"),
                ]
            )
            for anchors in anchors_by_source.values()
            for anchor in anchors
            if _canonical_phrase(
                [
                    anchor.get("boundary_condition"),
                    _as_mapping(anchor.get("support_envelope")).get("scope"),
                ]
            )
        }
        type_specific_passed = len(boundary_signatures) >= 2
        type_specific_explanation = f"The located anchors contain {len(boundary_signatures)} distinct boundary or scope signature(s)."
    elif relation_type == "methodological_fault_line":
        method_signatures = {
            _canonical_phrase(
                [
                    anchor.get("dimensions", {}).get("method", []),
                    anchor.get("evidence_role"),
                ]
            )
            for anchors in anchors_by_source.values()
            for anchor in anchors
            if _canonical_phrase(
                [
                    anchor.get("dimensions", {}).get("method", []),
                    anchor.get("evidence_role"),
                ]
            )
        }
        type_specific_passed = len(method_signatures) >= 2
        type_specific_explanation = f"The located anchors contain {len(method_signatures)} distinct method or evidence-role signature(s)."

    passed = bool(family_terms) and shared_object_passed and type_specific_passed
    return {
        "passed": passed,
        "bounded_object_terms": sorted(family_terms),
        "shared_object_terms": sorted(shared_object_terms),
        "shared_object_passed": shared_object_passed,
        "type_specific_passed": type_specific_passed,
        "type_specific_explanation": type_specific_explanation,
        "explanation": (
            "The located anchors share a bounded, non-generic analytical object."
            if passed
            else "The typed edge does not deterministically connect the located anchors to one bounded analytical object."
        ),
    }


def _proposal_membership_evidence(
    proposal: Mapping[str, Any],
    core_source_ids: Sequence[str],
    profile_by_source: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Resolve central located anchors for thematic membership only.

    Provider proposal packets can contain legacy anchor IDs or abbreviated
    locators because existing schema-1.0--1.4 profiles retain those aliases.
    That variation must not weaken strict claim validation, but it also must
    not erase a coherent subliterature.  For each provider-selected source,
    choose the best synthesis-eligible canonical anchor whose content and
    source-level semantic profile both address the bounded cluster topic.
    """

    family_terms = (
        _tokens(
            [
                proposal.get("bounded_object"),
                proposal.get("shared_question"),
                proposal.get("semantic_identity"),
                proposal.get("label"),
            ]
        )
        - _GENERIC_FAMILY_RELATION_TERMS
        - _BROAD_FIELD_TERMS
    )
    discriminating_family_terms = (
        family_terms
        - _THEMATIC_OUTCOME_TERMS
        - _THEMATIC_LINKING_GENERIC_TERMS
    )
    raw_proposal_terms = _tokens(
        [
            proposal.get("bounded_object"),
            proposal.get("shared_question"),
            proposal.get("semantic_identity"),
            proposal.get("label"),
        ]
    )
    target_terms = raw_proposal_terms & _BROAD_FIELD_TERMS
    outcome_terms = raw_proposal_terms & _THEMATIC_OUTCOME_TERMS
    outcome_centered_family = bool(target_terms and outcome_terms)
    supplied_by_source: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for reference in proposal.get("supporting_evidence", []) or []:
        if not isinstance(reference, Mapping):
            continue
        source_id = str(reference.get("source_id") or "")
        if source_id in core_source_ids:
            supplied_by_source[source_id].append(reference)

    def term_match(left: set[str], right: set[str]) -> tuple[set[str], bool]:
        direct = left & right
        outcome_alias = bool(left & _THEMATIC_OUTCOME_TERMS) and bool(
            right & _THEMATIC_OUTCOME_TERMS
        )
        return direct, outcome_alias

    selected: list[dict[str, Any]] = []
    source_assessments: list[dict[str, Any]] = []
    for source_id in core_source_ids:
        profile = profile_by_source.get(source_id)
        supplied = supplied_by_source.get(source_id, [])
        if profile is None or not supplied:
            source_assessments.append(
                {
                    "source_id": source_id,
                    "passed": False,
                    "reason": "missing_provider_selected_membership_evidence",
                }
            )
            continue
        profile_terms = (
            _tokens(
                [
                    profile.get("title"),
                    profile.get("research_questions"),
                    profile.get("thesis"),
                    profile.get("concepts"),
                    profile.get("theories"),
                    profile.get("mechanisms"),
                    profile.get("outcomes"),
                    profile.get("semantic_topic_labels"),
                    list(_as_mapping(profile.get("semantic_topic_scores"))),
                ]
            )
            - _GENERIC_FAMILY_RELATION_TERMS
        )
        profile_overlap, profile_outcome_alias = term_match(profile_terms, family_terms)
        profile_theme_overlap = profile_overlap & discriminating_family_terms
        profile_has_target = bool(profile_terms & target_terms)
        profile_has_outcome = bool(profile_terms & _THEMATIC_OUTCOME_TERMS)
        if discriminating_family_terms:
            profile_match_passed = bool(profile_theme_overlap) or bool(
                outcome_centered_family
                and profile_has_target
                and profile_has_outcome
            )
        else:
            profile_match_passed = bool(profile_overlap or profile_outcome_alias)
        if not profile_match_passed:
            source_assessments.append(
                {
                    "source_id": source_id,
                    "passed": False,
                    "reason": "cluster_topic_not_central_in_source_profile",
                }
            )
            continue

        supplied_ids = {
            str(
                reference.get("evidence_anchor_id")
                or reference.get("claim_id")
                or reference.get("finding_id")
                or ""
            )
            for reference in supplied
        }
        supplied_locators = {
            _normalized_locator(reference.get("locator") or "")
            for reference in supplied
            if _normalized_locator(reference.get("locator") or "")
        }
        candidates: list[
            tuple[tuple[int, int, int, int, str], Mapping[str, Any], set[str], bool]
        ] = []
        for anchor in profile.get("claims", []) or []:
            if not isinstance(anchor, Mapping) or not _anchor_is_synthesis_eligible(
                anchor
            ):
                continue
            anchor_terms = (
                _tokens(
                    [
                        anchor.get("text"),
                        anchor.get("claim"),
                        anchor.get("plain_english_meaning"),
                        anchor.get("topic"),
                        anchor.get("dimensions"),
                        _as_mapping(anchor.get("support_envelope")).get("scope"),
                    ]
                )
                - _GENERIC_FAMILY_RELATION_TERMS
            )
            anchor_overlap, anchor_outcome_alias = term_match(
                anchor_terms, family_terms
            )
            anchor_theme_overlap = anchor_overlap & discriminating_family_terms
            anchor_has_target = bool(anchor_terms & target_terms)
            anchor_has_outcome = bool(anchor_terms & _THEMATIC_OUTCOME_TERMS)
            relationship_terms = (
                anchor_terms
                - target_terms
                - _THEMATIC_OUTCOME_TERMS
                - _GENERIC_FAMILY_RELATION_TERMS
                - _BROAD_FIELD_TERMS
                - _THEMATIC_LINKING_GENERIC_TERMS
            )
            if discriminating_family_terms:
                anchor_match_passed = bool(anchor_theme_overlap) or bool(
                    outcome_centered_family
                    and anchor_has_target
                    and anchor_has_outcome
                    and relationship_terms
                )
            else:
                anchor_match_passed = bool(
                    anchor_overlap
                    or anchor_outcome_alias
                    or (
                        outcome_centered_family
                        and anchor_has_target
                        and anchor_has_outcome
                        and relationship_terms
                    )
                )
            if not anchor_match_passed:
                continue
            anchor_id = str(
                anchor.get("evidence_anchor_id") or anchor.get("claim_id") or ""
            )
            locator = _normalized_locator(anchor.get("locator") or "")
            locator_match = any(
                locator == supplied_locator
                or (
                    len(locator) >= 6
                    and len(supplied_locator) >= 6
                    and (
                        locator.startswith(supplied_locator)
                        or supplied_locator.startswith(locator)
                    )
                )
                for supplied_locator in supplied_locators
            )
            candidates.append(
                (
                    (
                        len(anchor_overlap),
                        int(anchor_outcome_alias),
                        int(anchor_id in supplied_ids),
                        int(locator_match),
                        anchor_id,
                    ),
                    anchor,
                    anchor_overlap,
                    anchor_outcome_alias,
                )
            )
        if not candidates:
            source_assessments.append(
                {
                    "source_id": source_id,
                    "passed": False,
                    "reason": "no_central_synthesis_eligible_anchor",
                }
            )
            continue
        _, anchor, anchor_overlap, anchor_outcome_alias = max(
            candidates, key=lambda row: row[0]
        )
        selected.append(_evidence_ref(anchor))
        source_assessments.append(
            {
                "source_id": source_id,
                "passed": True,
                "evidence_anchor_id": str(
                    anchor.get("evidence_anchor_id") or anchor.get("claim_id") or ""
                ),
                "matched_terms": sorted(anchor_overlap | profile_overlap),
                "outcome_alias_match": bool(
                    anchor_outcome_alias or profile_outcome_alias
                ),
            }
        )

    covered_source_ids = sorted(
        {str(reference.get("source_id") or "") for reference in selected}
    )
    assessment = {
        "passed": bool(family_terms) and len(covered_source_ids) >= 2,
        "bounded_object_terms": sorted(family_terms),
        "shared_object_terms": sorted(
            {
                term
                for row in source_assessments
                for term in row.get("matched_terms", []) or []
            }
        ),
        "shared_object_passed": len(covered_source_ids) >= 2,
        "type_specific_passed": len(covered_source_ids) >= 2,
        "type_specific_explanation": (
            f"{len(covered_source_ids)} provider-selected core sources have a central, synthesis-eligible located contribution."
        ),
        "source_assessments": source_assessments,
        "excluded_core_source_ids": sorted(
            set(core_source_ids) - set(covered_source_ids)
        ),
        "explanation": (
            "Each retained core source centrally addresses the bounded research problem through a canonical located anchor."
            if len(covered_source_ids) >= 2
            else "Fewer than two core sources have central located evidence for the bounded research problem."
        ),
    }
    return sorted(
        {_stable_hash(row): row for row in selected}.values(),
        key=lambda row: (row["source_id"], row["evidence_anchor_id"]),
    ), assessment


def _proposal_family_relations(
    proposal: Mapping[str, Any],
    admitted_propositions: Sequence[Mapping[str, Any]],
    profile_by_source: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Resolve embedded family relations without weakening anchor validation."""

    relations: list[dict[str, Any]] = []
    for raw in proposal.get("family_relations", []) or []:
        if not isinstance(raw, Mapping):
            continue
        relation_type = str(raw.get("relation_type") or "")
        if relation_type not in FAMILY_RELATION_TYPES:
            continue
        # Exact-proposition relations are generated only from propositions
        # that already passed the stricter proposition comparability gate.
        if relation_type == "same_proposition":
            continue
        source_ids = sorted(
            {
                str(value)
                for value in raw.get("source_ids", []) or []
                if str(value) in profile_by_source
            }
        )
        if len(source_ids) < 2:
            continue
        evidence = _resolve_reasoner_evidence(
            raw.get("evidence", []) or [],
            profile_by_source,
            allowed_source_ids=set(source_ids),
        )
        evidence = [
            reference
            for reference in evidence
            if _reference_is_synthesis_eligible(
                reference,
                profile_by_source[str(reference.get("source_id") or "")],
            )
        ]
        covered = {str(reference.get("source_id") or "") for reference in evidence}
        if not set(source_ids) <= covered:
            continue
        semantic_assessment = _family_relation_semantic_assessment(
            proposal,
            relation_type,
            source_ids,
            evidence,
            profile_by_source,
        )
        if not semantic_assessment["passed"]:
            continue
        relations.append(
            {
                "relation_type": relation_type,
                "source_ids": source_ids,
                "rationale": str(raw.get("rationale") or ""),
                "evidence": evidence,
                "comparability": {
                    "provider_assessment": dict(_as_mapping(raw.get("comparability"))),
                    **semantic_assessment,
                },
            }
        )

    # Existing reasoners remain compatible: an admitted exact proposition is
    # itself sufficient evidence for a same-proposition family relationship.
    for proposition in admitted_propositions:
        source_ids = sorted(
            {
                str(value)
                for value in proposition.get("source_ids", []) or []
                if str(value) in profile_by_source
            }
        )
        evidence = _resolve_reasoner_evidence(
            proposition.get("evidence", []) or [],
            profile_by_source,
            allowed_source_ids=set(source_ids),
        )
        covered = {str(reference.get("source_id") or "") for reference in evidence}
        if len(source_ids) < 2 or not set(source_ids) <= covered:
            continue
        relations.append(
            {
                "relation_type": "same_proposition",
                "source_ids": source_ids,
                "rationale": str(
                    proposition.get("statement")
                    or "The sources address the same locator-backed proposition."
                ),
                "evidence": evidence,
                "comparability": dict(_as_mapping(proposition.get("comparability"))),
            }
        )
    roles = _proposal_source_roles(proposal)
    core_source_ids = sorted(
        source_id
        for source_id in {str(value) for value in proposal.get("source_ids", []) or []}
        if source_id in profile_by_source and roles.get(source_id, "core") == "core"
    )
    shared_problem_covers_all_core_sources = any(
        relation.get("relation_type") == "shared_research_problem"
        and set(core_source_ids) <= set(relation.get("source_ids", []) or [])
        for relation in relations
    )
    if not shared_problem_covers_all_core_sources:
        membership_evidence, assessment = _proposal_membership_evidence(
            proposal,
            core_source_ids,
            profile_by_source,
        )
        covered = {
            str(reference.get("source_id") or "") for reference in membership_evidence
        }
        if assessment["passed"] and len(covered) >= 2:
            relations.append(
                {
                    "relation_type": "shared_research_problem",
                    "source_ids": sorted(covered),
                    "rationale": str(
                        proposal.get("coherence_rationale")
                        or "Located source findings centrally address one bounded research problem."
                    ),
                    "evidence": membership_evidence,
                    "comparability": assessment,
                }
            )
    return sorted(
        {_stable_hash(row): row for row in relations}.values(),
        key=lambda row: (row["relation_type"], row["source_ids"], _stable_hash(row)),
    )


def _family_relation_connected_components(
    core_source_ids: set[str],
    relations: Sequence[Mapping[str, Any]],
) -> list[set[str]]:
    """Return maximal components without inferring any relation edge."""

    if not core_source_ids:
        return []
    adjacency: dict[str, set[str]] = {source_id: set() for source_id in core_source_ids}
    for relation in relations:
        members = sorted(
            core_source_ids
            & {str(value) for value in relation.get("source_ids", []) or []}
        )
        for left, right in combinations(members, 2):
            adjacency[left].add(right)
            adjacency[right].add(left)
    components: list[set[str]] = []
    remaining = set(core_source_ids)
    while remaining:
        pending = [min(remaining)]
        component: set[str] = set()
        while pending:
            source_id = pending.pop()
            if source_id in component:
                continue
            component.add(source_id)
            pending.extend(sorted(adjacency[source_id] - component))
        components.append(component)
        remaining -= component
    return sorted(components, key=lambda row: (-len(row), sorted(row)))


def _family_relation_graph_connected(
    core_source_ids: set[str],
    relations: Sequence[Mapping[str, Any]],
) -> bool:
    components = _family_relation_connected_components(core_source_ids, relations)
    return (
        len(core_source_ids) >= 2
        and len(components) == 1
        and components[0] == core_source_ids
    )


def _qualify_provider_proposition(
    proposition: Mapping[str, Any],
    profile_by_source: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Narrow unsupported causal wording to a traceable source-attribution claim."""

    result = dict(proposition)
    statement = str(result.get("statement") or "").strip()
    if not _has_unqualified_causal_language(statement):
        return result
    causal_support_present = False
    for reference in result.get("evidence", []) or []:
        source_id = str(reference.get("source_id") or "")
        anchor_id = str(
            reference.get("evidence_anchor_id")
            or reference.get("claim_id")
            or reference.get("finding_id")
            or ""
        )
        profile = profile_by_source.get(source_id, {})
        anchor = next(
            (
                row
                for row in profile.get("claims", []) or []
                if str(row.get("evidence_anchor_id") or row.get("claim_id") or "")
                == anchor_id
            ),
            {},
        )
        causal_support_present = causal_support_present or _anchor_supports_causal_claim(
            anchor
        )
    if causal_support_present:
        return result
    proposition_type = str(result.get("proposition_type") or "").casefold()
    if proposition_type in {
        "practice_guidance",
        "practitioner",
        "guidance",
        "recommendation",
    }:
        prefix = "Practice-guidance sources advance the claim that "
    elif proposition_type in {"normative", "interpretive", "theoretical", "conceptual"}:
        prefix = "Normative or interpretive sources argue that "
    else:
        prefix = "The cited sources report or argue that "
    result["original_statement"] = statement
    result["statement"] = prefix + statement[:1].lower() + statement[1:]
    result["support_qualification"] = "causal_relationship_not_established"
    result["proposition_id"] = (
        "proposition-"
        + _stable_hash(
            {
                "statement": _canonical_phrase(result["statement"]),
                "source_ids": sorted(
                    str(value) for value in result.get("source_ids", []) or []
                ),
            }
        )[:16]
    )
    return result


_COMPARABILITY_TOKEN_ALIASES = {
    "biased": "bias",
    "biases": "bias",
    "inclusion": "participation",
    "intens": "intensity",
    "succeed": "success",
    "successful": "success",
    "succeeded": "success",
    "succeeds": "success",
}


def _comparability_token_sequence(value: Any) -> list[str]:
    """Return normalized relationship-bearing terms while retaining order."""

    text = " ".join(_flatten_values(value)).casefold()
    tokens = [
        _COMPARABILITY_TOKEN_ALIASES.get(stemmed, stemmed)
        for raw in re.findall(r"[a-z0-9]+", text)
        if raw not in _STOPWORDS and len(raw) > 2
        if (stemmed := _stem_token(raw))
    ]
    return [
        token
        for token in tokens
        if token not in _GENERIC_COMPARABILITY_TERMS and not token.isdigit()
    ]


def _comparability_tokens(value: Any) -> set[str]:
    """Return relationship-bearing terms rather than broad field vocabulary."""

    return set(_comparability_token_sequence(value))


def _proposition_subject_tokens(statement: Any) -> set[str]:
    tokens = (
        _comparability_tokens(statement)
        - _OUTCOME_SIGNAL_TERMS
        - _NON_DISCRIMINATING_RELATIONSHIP_TERMS
    )
    raw_tokens = set(
        re.findall(r"[a-z0-9]+", " ".join(_flatten_values(statement)).casefold())
    )
    # In inclusion propositions, participation is the treatment or mechanism,
    # even though participation can be an outcome in a different literature.
    if raw_tokens & {"inclusion", "inclusive"}:
        tokens.add("participation")
    return tokens


def _anchor_supports_proposition_statement(
    anchor: Mapping[str, Any], statement: str
) -> bool:
    """Require the cited anchor to address the proposition's actual relationship."""

    statement_tokens = _comparability_tokens(statement)
    # Topic/dimension/scope fields can be source-level retrieval metadata in
    # legacy profiles. They must never make an unrelated statistic support a
    # proposition; only the source-local claim text can do that.
    anchor_tokens = _comparability_tokens(anchor.get("text"))
    shared_tokens = statement_tokens & anchor_tokens
    if len(shared_tokens) < 2:
        return False
    statement_outcomes = statement_tokens & _OUTCOME_SIGNAL_TERMS
    if statement_outcomes and not (statement_outcomes & anchor_tokens):
        return False
    # Sharing only an outcome and generic relationship wording is not enough.
    # For example, "mediation duration is associated with success" must not
    # populate a row about conflict intensity merely because both findings say
    # "associated with mediation success". At least one proposition-specific
    # subject or mechanism must occur in the source-local anchor text.
    discriminating_tokens = _proposition_subject_tokens(statement)
    return bool(discriminating_tokens & anchor_tokens)


def _numeric_tokens(value: Any) -> set[str]:
    return {
        match.group(0).casefold().replace(" ", "")
        for match in re.finditer(r"(?<![A-Za-z])\d+(?:\.\d+)?\s*%?", str(value or ""))
    }


def _anchor_supports_thread_sentence(
    anchor: Mapping[str, Any], sentence: str
) -> bool:
    """Require claim-level semantic and numerical support for a thread sentence."""

    anchor_text = str(anchor.get("text") or anchor.get("claim") or "")
    if not anchor_text or not _anchor_is_synthesis_eligible(anchor):
        return False
    sentence_numbers = _numeric_tokens(sentence)
    if sentence_numbers and not (sentence_numbers & _numeric_tokens(anchor_text)):
        return False
    if _anchor_supports_proposition_statement(anchor, sentence):
        return True
    sentence_terms = _comparability_tokens(sentence)
    anchor_terms = _comparability_tokens(anchor_text)
    return bool(
        len(sentence_terms & anchor_terms) >= 3
        and _proposition_subject_tokens(sentence) & anchor_terms
    )


def _reconcile_evidence_thread_support(
    item: dict[str, Any],
    statement: str,
    evidence: Sequence[Mapping[str, Any]],
    profile_by_source: Mapping[str, Mapping[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Repair wrong thread locators or omit sentences that no anchor supports."""

    source_ids = sorted(
        {
            str(reference.get("source_id") or "")
            for reference in evidence
            if str(reference.get("source_id") or "")
        }
    )
    candidates_by_source: dict[str, list[Mapping[str, Any]]] = {}
    for source_id in source_ids:
        candidates = [
            anchor
            for anchor in profile_by_source.get(source_id, {}).get("claims", []) or []
            if _anchor_is_synthesis_eligible(anchor)
        ]
        specific = [
            anchor for anchor in candidates if not _anchor_is_composite_note_summary(anchor)
        ]
        candidates_by_source[source_id] = specific or candidates

    sentences = [
        value.strip()
        for value in re.split(r"(?<=[.!?])\s+", statement)
        if value.strip()
    ]
    sentence_results: list[tuple[str, list[dict[str, Any]], bool]] = []
    selected_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for sentence in sentences:
        sentence_numbers = _numeric_tokens(sentence)
        sentence_terms = _comparability_tokens(sentence)
        sentence_references: list[dict[str, Any]] = []
        sentence_anchors: list[Mapping[str, Any]] = []
        for source_id, candidates in candidates_by_source.items():
            supported = [
                anchor
                for anchor in candidates
                if _anchor_supports_thread_sentence(anchor, sentence)
            ]
            if not supported:
                continue
            best = max(
                supported,
                key=lambda anchor: (
                    len(sentence_numbers & _numeric_tokens(anchor.get("text"))),
                    len(sentence_terms & _comparability_tokens(anchor.get("text"))),
                    -len(str(anchor.get("text") or "")),
                ),
            )
            sentence_references.append(_evidence_ref(best))
            sentence_anchors.append(best)
        supported_numbers = {
            number
            for anchor in sentence_anchors
            for number in _numeric_tokens(anchor.get("text") or anchor.get("claim"))
        }
        if sentence_numbers and not sentence_numbers.issubset(supported_numbers):
            sentence_references = []
        organizational = bool(
            not sentence_numbers
            and re.search(
                r"\b(?:do not|does not|cannot|should remain|without (?:claiming|implying)|"
                r"different (?:angles|measures|methods|parts)|directly comparable|"
                r"bounded (?:problem|question)|preserves? (?:both|the)|limiting its verdict|"
                r"fit together|rather than|collection does not|evidence cannot|"
                r"bounded map|comparison is missing|without making)\b",
                sentence,
                flags=re.I,
            )
        )
        sentence_results.append((sentence, sentence_references, organizational))
        for reference in sentence_references:
            key = (
                str(reference.get("source_id") or ""),
                str(reference.get("evidence_anchor_id") or reference.get("claim_id") or ""),
            )
            selected_by_key[key] = reference
    has_supported_sentence = any(references for _, references, _ in sentence_results)
    supported_sentences = [
        sentence
        for sentence, references, organizational in sentence_results
        if references or (organizational and (has_supported_sentence or evidence))
    ]
    if not selected_by_key and supported_sentences:
        selected_by_key = {
            (
                str(reference.get("source_id") or ""),
                str(reference.get("evidence_anchor_id") or reference.get("claim_id") or ""),
            ): dict(reference)
            for reference in evidence
        }
    repaired_statement = " ".join(supported_sentences).strip()
    repaired_evidence = list(selected_by_key.values())
    if repaired_statement:
        _replace_cluster_item_statement(item, repaired_statement)
        item["source_ids"] = sorted(
            {str(reference.get("source_id") or "") for reference in repaired_evidence}
        )
    return repaired_statement, repaired_evidence


def _neutralize_directional_proposition(statement: str) -> str:
    neutral = re.sub(
        r"\b(?:fewer|greater|higher|lower|more|less|negative(?:ly)?|positive(?:ly)?)\b",
        "",
        statement,
        flags=re.I,
    )
    neutral = re.sub(r"\s+", " ", neutral)
    neutral = re.sub(r"\s+([,.;:])", r"\1", neutral)
    neutral = neutral.strip()
    return neutral[:1].upper() + neutral[1:] if neutral else neutral


def _precise_proposition_references(
    statement: str,
    source_ids: set[str],
    profile_by_source: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Select one exact, jointly comparable anchor per participating source.

    Provider references are hypotheses, not authority. A broad table-summary
    anchor can contain every word in a compound proposition while obscuring
    which source-local finding actually supplies the comparison. This selector
    finds a proposition-specific subject shared by independent studies, then
    chooses the shortest eligible anchor that states that relationship for each
    source. Sources without the shared relationship fall out of the row.
    """

    statement_tokens = _comparability_tokens(statement)
    discriminating_tokens = _proposition_subject_tokens(statement)
    candidates_by_source: dict[str, list[Mapping[str, Any]]] = {}
    for source_id in sorted(source_ids):
        profile = profile_by_source.get(source_id)
        if profile is None:
            continue
        candidates = [
            anchor
            for anchor in profile.get("claims", []) or []
            if _anchor_is_synthesis_eligible(anchor)
            and _anchor_supports_proposition_statement(anchor, statement)
        ]
        if candidates:
            candidates_by_source[source_id] = candidates

    ambiguous_singletons = {
        "actor",
        "addressing",
        "bias",
        "commitment",
        "compared",
        "government",
        "group",
        "institution",
        "party",
        "problem",
        "rebel",
        "state",
    }

    def subject_features(value: Any) -> set[tuple[str, ...]]:
        sequence = [
            token
            for token in _comparability_token_sequence(value)
            if token in discriminating_tokens
        ]
        features = {(token,) for token in sequence if token not in ambiguous_singletons}
        features.update(
            (left, right)
            for left, right in zip(sequence, sequence[1:])
            if left != right
        )
        return features

    statement_features = subject_features(statement)
    feature_families: dict[tuple[str, ...], set[str]] = defaultdict(set)
    for source_id, anchors in candidates_by_source.items():
        family_id = str(
            profile_by_source[source_id].get("study_family_id") or source_id
        )
        for anchor in anchors:
            for feature in statement_features & subject_features(anchor.get("text")):
                feature_families[feature].add(family_id)
    shared_features = {
        feature: families
        for feature, families in feature_families.items()
        if len(families) >= 2
    }
    if not shared_features:
        return [], {"passed": False, "reason": "no_shared_proposition_subject"}

    selections: list[
        tuple[tuple[int, int, int, str], tuple[str, ...], list[Mapping[str, Any]]]
    ] = []
    for feature, families in shared_features.items():
        selected: list[Mapping[str, Any]] = []
        for source_id, anchors in candidates_by_source.items():
            matching = [
                anchor
                for anchor in anchors
                if feature in subject_features(anchor.get("text"))
            ]
            if not matching:
                continue
            selected.append(
                min(
                    matching,
                    key=lambda anchor: (
                        len(_comparability_tokens(anchor.get("text"))),
                        -len(
                            statement_tokens & _comparability_tokens(anchor.get("text"))
                        ),
                        str(
                            anchor.get("evidence_anchor_id")
                            or anchor.get("claim_id")
                            or ""
                        ),
                    ),
                )
            )
        selected_families = {
            str(anchor.get("study_family_id") or anchor.get("source_id") or "")
            for anchor in selected
        }
        if len(selected_families) < 2:
            continue
        selections.append(
            (
                (
                    -len(selected_families),
                    -len(feature),
                    sum(
                        len(_comparability_tokens(anchor.get("text")))
                        for anchor in selected
                    ),
                    " ".join(feature),
                ),
                feature,
                selected,
            )
        )
    if not selections:
        return [], {
            "passed": False,
            "reason": "no_independent_shared_proposition_subject",
        }

    _, shared_feature, selected = min(selections, key=lambda row: row[0])
    references = [_evidence_ref(anchor) for anchor in selected]
    common_anchor_tokens = set.intersection(
        *(_comparability_tokens(anchor.get("text")) for anchor in selected)
    )
    unshared_statement_tokens = (
        discriminating_tokens | (statement_tokens & _OUTCOME_SIGNAL_TERMS)
    ) - common_anchor_tokens
    narrowed_statement = ""
    if unshared_statement_tokens:
        narrowed_statement = min(
            (str(anchor.get("text") or "").strip() for anchor in selected),
            key=lambda value: (
                len(_comparability_tokens(value)),
                len(value),
                value.casefold(),
            ),
        )
    return references, {
        "passed": True,
        "shared_proposition_subject": " ".join(shared_feature),
        "narrowed_statement": narrowed_statement,
        "selected_directions": sorted(
            {str(anchor.get("direction") or "not_reported") for anchor in selected}
        ),
        "unshared_provider_terms": sorted(unshared_statement_tokens),
    }


def _provider_comparable_cells(
    cells: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Keep only cells that actually address one shared bounded proposition.

    Provider proposals are useful hypotheses, but a model can accidentally put
    adjacent claims into one row.  Outcome-bearing empirical cells therefore
    need a shared non-generic outcome.  Conceptual rows without outcomes need
    at least two shared relationship-bearing terms.  This deliberately favors
    false negatives over false analytical clusters.
    """

    rows = [dict(cell) for cell in cells if isinstance(cell, Mapping)]
    if len(rows) < 2:
        return [], {"passed": False, "reason": "fewer_than_two_source_cells"}

    outcome_tokens: dict[str, set[str]] = {}
    for cell in rows:
        source_id = str(cell.get("source_id") or "")
        tokens = _comparability_tokens(
            _as_mapping(cell.get("scope")).get("outcome", [])
        )
        if not tokens:
            tokens = (
                _comparability_tokens(cell.get("stance_or_finding", ""))
                & _OUTCOME_SIGNAL_TERMS
            )
        outcome_tokens[source_id] = tokens
    cells_with_outcomes = {
        source_id: tokens for source_id, tokens in outcome_tokens.items() if tokens
    }
    if cells_with_outcomes:
        token_families: dict[str, set[str]] = defaultdict(set)
        for cell in rows:
            source_id = str(cell.get("source_id") or "")
            family_id = str(cell.get("study_family_id") or source_id)
            for token in outcome_tokens.get(source_id, set()):
                token_families[token].add(family_id)
        shared = {
            token: families
            for token, families in token_families.items()
            if len(families) >= 2
        }
        if not shared:
            return [], {"passed": False, "reason": "no_shared_bounded_outcome"}
        strongest = max(len(families) for families in shared.values())
        admitted_terms = sorted(
            token for token, families in shared.items() if len(families) == strongest
        )
        selected = [
            cell
            for cell in rows
            if outcome_tokens.get(str(cell.get("source_id") or ""), set())
            & set(admitted_terms)
        ]
        if (
            len(
                {
                    str(cell.get("study_family_id") or cell.get("source_id"))
                    for cell in selected
                }
            )
            < 2
        ):
            return [], {"passed": False, "reason": "no_independent_shared_outcome"}
        return selected, {
            "passed": True,
            "basis": "shared_bounded_outcome",
            "shared_outcome_terms": admitted_terms,
        }

    stance_tokens = {
        str(cell.get("source_id") or ""): _comparability_tokens(
            cell.get("stance_or_finding", "")
        )
        for cell in rows
    }
    pair_families: dict[tuple[str, str], set[str]] = defaultdict(set)
    for cell in rows:
        source_id = str(cell.get("source_id") or "")
        family_id = str(cell.get("study_family_id") or source_id)
        for token_pair in combinations(sorted(stance_tokens.get(source_id, set())), 2):
            pair_families[token_pair].add(family_id)
    shared_pairs = {
        pair: families for pair, families in pair_families.items() if len(families) >= 2
    }
    if not shared_pairs:
        return [], {"passed": False, "reason": "no_shared_conceptual_relationship"}
    strongest = max(len(families) for families in shared_pairs.values())
    admitted_pair = min(
        pair for pair, families in shared_pairs.items() if len(families) == strongest
    )
    selected = [
        cell
        for cell in rows
        if set(admitted_pair)
        <= stance_tokens.get(str(cell.get("source_id") or ""), set())
    ]
    return selected, {
        "passed": True,
        "basis": "shared_conceptual_relationship",
        "shared_relationship_terms": list(admitted_pair),
    }


def _proposal_propositions(
    proposal: Mapping[str, Any],
    propositions: Sequence[Mapping[str, Any]],
    profile_by_source: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    source_ids = {
        str(value) for value in proposal.get("source_ids", []) or [] if str(value)
    }
    proposal_evidence = [
        dict(reference)
        for reference in proposal.get("supporting_evidence", []) or []
        if isinstance(reference, Mapping)
    ]
    invalid_proposal_evidence = bool(proposal_evidence) and any(
        (profile := profile_by_source.get(str(reference.get("source_id") or "")))
        is None
        or not _reference_matches_profile(reference, profile)
        for reference in proposal_evidence
    )
    supplied = [
        row
        for row in proposal.get("propositions", []) or []
        if isinstance(row, Mapping)
    ]
    proposition_by_id = {
        str(row.get("proposition_id") or ""): row for row in propositions
    }
    matched: list[dict[str, Any]] = []
    for raw in supplied:
        known = proposition_by_id.get(str(raw.get("proposition_id") or ""))
        if known is not None:
            # Preserve deterministic/legacy proposition identities. Provider-authored
            # proposition text is narrowed below before it receives a map-local ID.
            matched.append(dict(known))
            continue
        statement = str(raw.get("statement") or raw.get("proposition") or "").strip()
        if not statement:
            continue
        references: list[dict[str, Any]] = []
        for reference in raw.get("evidence", raw.get("supporting_evidence", [])) or []:
            if not isinstance(reference, Mapping):
                continue
            profile = profile_by_source.get(str(reference.get("source_id") or ""))
            if profile is None or not _reference_matches_profile(reference, profile):
                continue
            claim_id = str(
                reference.get("evidence_anchor_id")
                or reference.get("claim_id")
                or reference.get("finding_id")
                or ""
            )
            claim = next(
                (
                    item
                    for item in profile.get("claims", []) or []
                    if str(item.get("evidence_anchor_id") or item.get("claim_id") or "")
                    == claim_id
                ),
                None,
            )
            if (
                claim is None
                or not _anchor_is_synthesis_eligible(claim)
                or not _anchor_supports_proposition_statement(claim, statement)
            ):
                continue
            references.append(dict(reference))
        expanded_source_ids: list[str] = []
        declared_source_ids = {
            str(value)
            for value in raw.get("source_ids", []) or []
            if str(value) in profile_by_source
        }
        referenced_source_ids = {
            str(reference.get("source_id") or "") for reference in references
        }
        for source_id in sorted(declared_source_ids - referenced_source_ids):
            candidates = [
                anchor
                for anchor in profile_by_source[source_id].get("claims", []) or []
                if _anchor_is_synthesis_eligible(anchor)
                and _anchor_supports_proposition_statement(anchor, statement)
            ]
            candidates.sort(
                key=lambda anchor: (
                    -len(
                        _comparability_tokens(statement)
                        & _comparability_tokens(anchor.get("text"))
                    ),
                    str(
                        anchor.get("evidence_anchor_id") or anchor.get("claim_id") or ""
                    ),
                )
            )
            if not candidates:
                continue
            references.extend(_evidence_ref(anchor) for anchor in candidates[:3])
            expanded_source_ids.append(source_id)
        provider_reference_ids = {
            str(reference.get("source_id") or ""): str(
                reference.get("evidence_anchor_id")
                or reference.get("claim_id")
                or reference.get("finding_id")
                or ""
            )
            for reference in references
        }
        references, precision = _precise_proposition_references(
            statement,
            declared_source_ids or set(provider_reference_ids),
            profile_by_source,
        )
        if not references:
            continue
        repaired_source_ids = {
            str(reference.get("source_id") or "")
            for reference in references
            if provider_reference_ids.get(str(reference.get("source_id") or ""))
            != str(
                reference.get("evidence_anchor_id") or reference.get("claim_id") or ""
            )
        }
        expanded_source_ids = sorted(set(expanded_source_ids) | repaired_source_ids)
        provider_statement = statement
        if str(precision.get("narrowed_statement") or "").strip():
            statement = str(precision["narrowed_statement"]).strip()
        selected_directions = {
            str(value) for value in precision.get("selected_directions", []) or []
        }
        if (
            "null" in selected_directions
            or len(selected_directions - {"not_reported"}) > 1
        ):
            statement = _neutralize_directional_proposition(statement)
        supplied_comparability = _as_mapping(raw.get("comparability"))
        if supplied_comparability.get("passed") is False:
            continue
        cells, deterministic_comparability = _provider_comparable_cells(
            _proposition_cells_from_references(references, profile_by_source)
        )
        admitted_source_ids = {str(cell.get("source_id") or "") for cell in cells}
        references = [
            reference
            for reference in references
            if str(reference.get("source_id") or "") in admitted_source_ids
        ]
        families = {
            str(
                profile_by_source[str(reference["source_id"])].get("study_family_id")
                or reference["source_id"]
            )
            for reference in references
        }
        if len(families) < 2:
            continue
        signature = {
            "statement": _canonical_phrase(statement),
            "source_ids": sorted(
                {str(reference["source_id"]) for reference in references}
            ),
        }
        candidate = _qualify_provider_proposition(
            {
                "proposition_id": f"proposition-{_stable_hash(signature)[:16]}",
                "semantic_identity": str(raw.get("semantic_identity") or ""),
                "statement": statement,
                "question": str(raw.get("question") or ""),
                "proposition_type": str(raw.get("proposition_type") or "unknown"),
                "source_ids": signature["source_ids"],
                "study_family_ids": sorted(families),
                "independent_study_family_count": len(families),
                "evidence": references,
                "cells": cells,
                "comparability": {
                    **supplied_comparability,
                    **deterministic_comparability,
                    **precision,
                    "provider_assessment": supplied_comparability,
                    "provider_statement": provider_statement,
                    "deterministic_anchor_expansion_source_ids": expanded_source_ids,
                    # Provider-authored direction labels are not assumed to
                    # share an orientation. The synthesis prose must make an
                    # actual positive/negative disagreement explicit.
                    "direction_orientation_aligned": False,
                },
            },
            profile_by_source,
        )
        if candidate.get("original_statement") and provider_statement != statement:
            candidate["original_statement"] = provider_statement
        matched.append(candidate)
    if matched:
        return sorted(
            {_stable_hash(row): row for row in matched}.values(),
            key=lambda row: row["proposition_id"],
        )

    if supplied:
        # An explicit provider proposition that fails support or comparability
        # cannot be rescued as a broader deterministic topic grouping.
        return []

    if invalid_proposal_evidence:
        # A proposal with no valid proposition row cannot be rescued by broad
        # source membership when its representative evidence is invented or
        # untraceable. Valid proposition rows above remain authoritative and
        # may survive an unrelated bad representative reference.
        return []

    # Legacy/custom reasoners may still submit only source IDs. They can use a
    # single deterministic proposition already proved from those sources. If
    # the source set spans multiple propositions, the proposal is only a broad
    # topic bin and must not fuse them into one analytical cluster.
    for proposition in propositions:
        participants = set(
            str(value) for value in proposition.get("source_ids", []) or []
        )
        if len(participants & source_ids) >= 2:
            matched.append(dict(proposition))
    if len(matched) != 1:
        return []
    return matched


def _proposition_cells_from_references(
    references: Sequence[Mapping[str, Any]],
    profile_by_source: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    anchors_by_source: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for reference in references:
        source_id = str(reference.get("source_id") or "")
        profile = profile_by_source.get(source_id)
        if profile is None:
            continue
        anchor_id = str(
            reference.get("evidence_anchor_id")
            or reference.get("claim_id")
            or reference.get("finding_id")
            or ""
        )
        anchor = next(
            (
                item
                for item in profile.get("claims", []) or []
                if str(item.get("evidence_anchor_id") or item.get("claim_id") or "")
                == anchor_id
                and _anchor_is_synthesis_eligible(item)
            ),
            None,
        )
        if anchor is not None:
            anchors_by_source[source_id].append(anchor)

    cells: list[dict[str, Any]] = []
    for source_id, anchors in sorted(anchors_by_source.items()):
        cells.append(
            {
                "source_id": source_id,
                "study_family_id": str(anchors[0].get("study_family_id") or source_id),
                "stance_or_finding": "; ".join(
                    dict.fromkeys(
                        str(anchor.get("text") or "").strip()
                        for anchor in anchors
                        if str(anchor.get("text") or "").strip()
                    )
                ),
                "evidence_type": sorted(
                    {
                        str(anchor.get("evidence_role") or "support_unknown")
                        for anchor in anchors
                    }
                ),
                "scope": {
                    key: sorted(
                        {
                            value
                            for anchor in anchors
                            for value in _anchor_scope_values(anchor, key)
                        }
                    )
                    for key in ("population", "case", "geography", "period", "outcome")
                },
                "boundary_conditions": sorted(
                    {
                        str(anchor.get("boundary_condition") or "")
                        for anchor in anchors
                        if str(anchor.get("boundary_condition") or "").strip()
                    }
                ),
                "direction_or_interpretation": sorted(
                    {
                        str(anchor.get("direction") or "not_reported")
                        for anchor in anchors
                    }
                ),
                "uncertainty": sorted(
                    {
                        str(anchor.get("uncertainty") or "")
                        for anchor in anchors
                        if str(anchor.get("uncertainty") or "").strip()
                    }
                ),
                "evidence": [_evidence_ref(anchor) for anchor in anchors],
            }
        )
    return cells


def _specific_unclustered_reason(
    source_id: str,
    profile: Mapping[str, Any],
    rejected: Sequence[Mapping[str, Any]],
    topic_neighborhoods: Sequence[Mapping[str, Any]] | None,
) -> tuple[str, str]:
    source_rejections = [
        str(row.get("reason") or "")
        for row in rejected
        if source_id in {str(value) for value in row.get("source_ids", []) or []}
        and str(row.get("reason") or "")
    ]
    detail = source_rejections[0] if source_rejections else ""
    normalized = detail.casefold()
    if "membership" in normalized and "limit" in normalized:
        return "membership_limit_exceeded", detail
    if "independent" in normalized or "evidence_base" in normalized:
        return "insufficient_independent_evidence_bases", detail
    if "locator" in normalized or "anchor" in normalized or "support" in normalized:
        return "no_central_locator_backed_membership_anchor", detail
    if "incompar" in normalized or "bounded_object" in normalized:
        return "incomparable_research_problem", detail
    if not any(
        _anchor_is_synthesis_eligible(claim) for claim in profile.get("claims", []) or []
    ):
        return (
            "no_central_locator_backed_membership_anchor",
            detail or "No synthesis-eligible locator-backed finding supports cluster membership.",
        )
    neighborhood_peers = {
        str(value)
        for neighborhood in topic_neighborhoods or []
        if isinstance(neighborhood, Mapping)
        and source_id
        in {str(item) for item in neighborhood.get("source_ids", []) or []}
        for value in neighborhood.get("source_ids", []) or []
        if str(value) and str(value) != source_id
    }
    if neighborhood_peers:
        return (
            "broad_topical_overlap_only",
            detail
            or "The source shares retrieval signals with other studies, but no bounded research conversation passed admission.",
        )
    return (
        "singleton_bounded_literature",
        detail
        or "No second independent analytical source addresses a sufficiently connected research problem.",
    )


def map_overlapping_clusters(
    profiles: Sequence[Any],
    relations: Sequence[Mapping[str, Any]] | None = None,
    *,
    policy: Any = None,
    proposals: Sequence[Mapping[str, Any]] | None = None,
    propositions: Sequence[Mapping[str, Any]] | None = None,
    topic_neighborhoods: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Admit connected debate families; validate comparable propositions separately."""

    rows = _ensure_profiles(profiles)
    analytical = [row for row in rows if row.get("analytical")]
    profile_by_source = {str(row["source_id"]): row for row in analytical}
    proposition_rows = [
        dict(row) for row in (propositions or build_literature_propositions(rows))
    ]
    min_backed = max(3, int(_policy_value(policy, "source_backed_threshold", 3)))
    min_emerging = 2
    max_memberships = max(1, min(3, int(_policy_value(policy, "max_memberships", 3))))
    auto_promote = bool(_policy_value(policy, "auto_promote_clusters", True))
    rejected: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    component_actions: list[dict[str, Any]] = []

    proposal_rows = [dict(row) for row in proposals or [] if isinstance(row, Mapping)]
    if not proposal_rows:
        proposal_rows = [
            {
                "proposal_id": f"proposal-{row['proposition_id']}",
                "label": row.get("statement") or row["proposition_id"],
                "semantic_identity": " ".join(
                    row.get("signature", {}).get("topic", [])
                    or row.get("signature", {}).get("outcome", [])
                    or row.get("signature", {}).get("relationship", [])
                ),
                "shared_question": row.get("question") or "",
                "coherence_rationale": "Independent sources address the same located proposition.",
                "source_ids": row.get("source_ids", []),
                "source_roles": {
                    source_id: "core" for source_id in row.get("source_ids", []) or []
                },
                "propositions": [row],
                "formation_route": "deterministic_proposition",
            }
            for row in proposition_rows
        ]

    for proposal in proposal_rows:
        admitted = _proposal_propositions(proposal, proposition_rows, profile_by_source)
        roles = _proposal_source_roles(proposal)
        proposal_sources = {
            str(value)
            for value in proposal.get("source_ids", []) or []
            if str(value) in profile_by_source
        }
        provider_declared_core_sources = {
            source_id
            for source_id in proposal_sources
            if roles.get(source_id, "core") == "core"
        }
        membership_evidence, membership_assessment = _proposal_membership_evidence(
            proposal,
            sorted(proposal_sources),
            profile_by_source,
        )
        membership_evidence_by_source = {
            str(reference.get("source_id") or ""): dict(reference)
            for reference in membership_evidence
            if reference.get("source_id")
        }
        bounded_membership_source_ids = {
            str(row.get("source_id") or "")
            for row in membership_assessment.get("source_assessments", []) or []
            if isinstance(row, Mapping) and row.get("passed") is True
        }
        family_relations = _proposal_family_relations(
            proposal, admitted, profile_by_source
        )
        relation_sources = {
            str(value)
            for relation in family_relations
            for value in relation.get("source_ids", []) or []
        }
        proposal_target_terms = (
            _tokens(
                [
                    proposal.get("label"),
                    proposal.get("shared_question"),
                    proposal.get("bounded_object"),
                    proposal.get("semantic_identity"),
                ]
            )
            & _BROAD_FIELD_TERMS
        )
        # A reasoner may initially mark a neighboring publication as core even
        # though its exact outcome does not enter the admitted family relation.
        # Preserve it as context only when its own located evidence still
        # addresses the bounded target field; unrelated records disappear and
        # retained context never counts toward debate or gap thresholds.
        for source_id in proposal_sources - relation_sources:
            profile = profile_by_source[source_id]
            profile_target_terms = _tokens(
                [
                    profile.get("title"),
                    profile.get("research_questions"),
                    profile.get("semantic_topic_labels"),
                    [anchor.get("text") for anchor in profile.get("claims", []) or []],
                ]
            ) & _BROAD_FIELD_TERMS
            if (
                roles.get(source_id, "core") == "core"
                and (
                    source_id in bounded_membership_source_ids
                    or proposal_target_terms & profile_target_terms
                )
                and any(
                    _anchor_is_synthesis_eligible(anchor)
                    for anchor in profile.get("claims", []) or []
                )
            ):
                roles[source_id] = "context"
        core_sources = {
            source_id
            for source_id in proposal_sources & relation_sources
            if roles.get(source_id, "core") == "core"
        }

        # Practitioner guidance cannot become core evidence for an empirical
        # effectiveness proposition.
        empirical_effectiveness = any(
            str(row.get("proposition_type") or "") == "empirical" for row in admitted
        )
        if empirical_effectiveness:
            for source_id in list(core_sources):
                source_role = str(
                    profile_by_source[source_id].get("source_role") or ""
                ).casefold()
                anchor_roles = {
                    str(
                        _as_mapping(anchor.get("support_envelope")).get("argument_role")
                        or "none"
                    )
                    for anchor in profile_by_source[source_id].get("claims", []) or []
                }
                if "practitioner" in source_role or anchor_roles == {
                    "practitioner_guidance"
                }:
                    core_sources.remove(source_id)
                    roles[source_id] = "context"

        declared_core_sources = {
            source_id
            for source_id in proposal_sources
            if roles.get(source_id, "core") == "core"
        }

        validated_relations = list(family_relations)
        core_relations = [
            relation
            for relation in family_relations
            if len(set(relation.get("source_ids", []) or []) & core_sources) >= 2
        ]
        components = [
            component
            for component in _family_relation_connected_components(
                core_sources, core_relations
            )
            if len(component) >= min_emerging
        ]
        # An otherwise coherent thematic proposal can contain a publication
        # that does not participate in the stricter proposition/relation
        # component. When the provider supplied a valid locator-backed
        # membership anchor, retain that publication as context rather than
        # falsely reporting it as unrelated to the literature. This does not
        # rescue a proposal with no valid multi-source component.
        connected_core_sources = set().union(*components) if components else set()
        for source_id in provider_declared_core_sources - connected_core_sources:
            if source_id in bounded_membership_source_ids:
                roles[source_id] = "context"
        if not components:
            rejected.append(
                {
                    "proposal_id": str(proposal.get("proposal_id") or ""),
                    "semantic_identity": str(
                        proposal.get("semantic_identity") or proposal.get("label") or ""
                    ),
                    "source_ids": sorted(proposal_sources),
                    "action": "reject",
                    "reason": "no_valid_connected_family_relation",
                }
            )
            continue
        split_required = (
            len(components) > 1
            or components[0] != core_sources
            or core_sources != declared_core_sources
        )
        admitted_component_rows: list[dict[str, Any]] = []
        for component_index, component in enumerate(components, start=1):
            component_relations = [
                {
                    **dict(relation),
                    "source_ids": sorted(
                        set(relation.get("source_ids", []) or []) & component
                    ),
                    "evidence": [
                        dict(reference)
                        for reference in relation.get("evidence", []) or []
                        if str(reference.get("source_id") or "") in component
                    ],
                }
                for relation in core_relations
                if len(set(relation.get("source_ids", []) or []) & component) >= 2
            ]
            if not _family_relation_graph_connected(component, component_relations):
                continue
            component_propositions = [
                row
                for row in admitted
                if len(
                    {str(value) for value in row.get("source_ids", []) or []}
                    & component
                )
                >= min_emerging
                and bool(_as_mapping(row.get("comparability")).get("passed", True))
            ]
            if not component_propositions and not component_relations:
                continue
            component_families = {
                str(profile_by_source[source_id].get("study_family_id") or source_id)
                for source_id in component
            }
            component_evidence_bases = {
                evidence_base_id
                for source_id in component
                if (
                    evidence_base_id := _profile_evidence_base_id(
                        profile_by_source[source_id]
                    )
                )
            }
            context_sources = {
                source_id
                for source_id in proposal_sources
                if roles.get(source_id) == "context"
            }
            bridge_sources = {
                source_id
                for source_id in proposal_sources
                if roles.get(source_id) == "bridge"
            }
            # Preserve the parent literature's human label and question when
            # admission merely narrows one weak source.  Invent a component
            # label only when the proposal truly splits into multiple
            # surviving literatures.
            if len(components) > 1:
                related_auxiliary_sources = {
                    source_id
                    for relation in validated_relations
                    if set(relation.get("source_ids", []) or []) & component
                    for source_id in set(relation.get("source_ids", []) or [])
                    - component
                    if source_id in context_sources or source_id in bridge_sources
                }
                component_terms = (
                    _tokens(
                        [
                            [
                                row.get("statement"),
                                row.get("question"),
                                row.get("semantic_identity"),
                            ]
                            for row in component_propositions
                        ]
                    )
                    - _GENERIC_FAMILY_RELATION_TERMS
                    - _BROAD_FIELD_TERMS
                )
                for source_id in (context_sources | bridge_sources):
                    if source_id not in bounded_membership_source_ids:
                        continue
                    reference = membership_evidence_by_source.get(source_id, {})
                    anchor_id = str(
                        reference.get("evidence_anchor_id")
                        or reference.get("claim_id")
                        or ""
                    )
                    anchor = next(
                        (
                            row
                            for row in profile_by_source[source_id].get("claims", [])
                            or []
                            if str(
                                row.get("evidence_anchor_id")
                                or row.get("claim_id")
                                or ""
                            )
                            == anchor_id
                        ),
                        {},
                    )
                    auxiliary_terms = (
                        _tokens(
                            [
                                anchor.get("text"),
                                anchor.get("topic"),
                                anchor.get("dimensions"),
                                profile_by_source[source_id].get("title"),
                                profile_by_source[source_id].get(
                                    "semantic_topic_labels"
                                ),
                            ]
                        )
                        - _GENERIC_FAMILY_RELATION_TERMS
                        - _BROAD_FIELD_TERMS
                    )
                    if component_terms & auxiliary_terms:
                        related_auxiliary_sources.add(source_id)
                context_sources &= related_auxiliary_sources
                bridge_sources &= related_auxiliary_sources
            component_proposal = dict(proposal)
            lineage = {
                "proposition_ids": sorted(
                    str(row.get("proposition_id") or "")
                    for row in component_propositions
                ),
                "family_relations": [
                    {
                        "relation_type": str(row.get("relation_type") or ""),
                        "source_ids": sorted(
                            str(value) for value in row.get("source_ids", []) or []
                        ),
                        "evidence": sorted(
                            (
                                str(reference.get("source_id") or ""),
                                str(
                                    reference.get("evidence_anchor_id")
                                    or reference.get("claim_id")
                                    or ""
                                ),
                            )
                            for reference in row.get("evidence", []) or []
                        ),
                    }
                    for row in component_relations
                ],
            }
            if len(components) > 1:
                component_summaries = list(
                    dict.fromkeys(
                        str(
                            row.get("semantic_identity")
                            or _as_mapping(row.get("comparability")).get(
                                "provider_statement"
                            )
                            or row.get("statement")
                            or ""
                        ).strip(" .")
                        for row in component_propositions
                        if str(
                            row.get("semantic_identity")
                            or _as_mapping(row.get("comparability")).get(
                                "provider_statement"
                            )
                            or row.get("statement")
                            or ""
                        ).strip()
                    )
                )
                if not component_summaries:
                    component_summaries = [
                        str(
                            component_relations[0].get("rationale")
                            or "Connected debate family"
                        ).strip(" .")
                    ]
                component_summary = "; ".join(component_summaries)
                if len(component_summaries) == 1:
                    component_label = component_summaries[0]
                else:
                    concise_summaries = []
                    for summary in component_summaries:
                        subject = re.split(
                            r"\b(?:affects?|determines?|influences?|predicts?|shapes?|explains?|increases?|reduces?)\b",
                            summary,
                            maxsplit=1,
                            flags=re.I,
                        )[0].strip(" ,;:-")
                        concise_summaries.append(subject or summary)
                    joined_summaries = (
                        concise_summaries[0]
                        + " and "
                        + " and ".join(
                            summary[:1].lower() + summary[1:]
                            for summary in concise_summaries[1:]
                        )
                    )
                    component_label = (
                        f"{str(proposal.get('label') or 'Connected debate family')}: "
                        + joined_summaries
                    )
                component_label = component_label[:117].rstrip() + (
                    "..." if len(component_label) > 117 else ""
                )
                component_questions = list(
                    dict.fromkeys(
                        str(row.get("question") or "").strip()
                        for row in component_propositions
                        if str(row.get("question") or "").strip()
                    )
                )
                component_question = (
                    " / ".join(component_questions)
                    if component_questions
                    else f"How are the located positions in {component_label} related?"
                )
                component_proposal.update(
                    {
                        "proposal_id": (
                            f"{str(proposal.get('proposal_id') or 'proposal')}--component-"
                            f"{_stable_hash(lineage)[:10]}"
                        ),
                        "label": component_label,
                        "semantic_identity": _canonical_phrase(component_summary)
                        or f"debate family {_stable_hash(lineage)[:12]}",
                        "shared_question": component_question,
                        "bounded_object": component_summary,
                        "coherence_rationale": (
                            "Deterministic admission retained this maximal connected component after unsupported or "
                            "disconnected parent-proposal sources were removed. Every retained edge remains locator-backed."
                        ),
                        "formation_route": "reasoner_debate_family_component",
                        "parent_proposal_id": str(proposal.get("proposal_id") or ""),
                    }
                )
            semantic_identity = _canonical_phrase(
                component_proposal.get("semantic_identity")
                or component_proposal.get("shared_question")
                or lineage
            ) or _stable_hash(lineage)
            all_sources = sorted(component | context_sources | bridge_sources)
            candidates.append(
                {
                    "proposal": component_proposal,
                    "semantic_identity": semantic_identity,
                    "component_lineage": lineage,
                    "propositions": component_propositions,
                    "family_relations": component_relations,
                    "core_source_ids": sorted(component),
                    "context_source_ids": sorted(context_sources),
                    "bridge_source_ids": sorted(bridge_sources),
                    "source_ids": all_sources,
                    "core_family_count": len(component_families),
                    "core_evidence_base_count": len(component_evidence_bases),
                }
            )
            admitted_component_rows.append(
                {
                    "component_index": component_index,
                    "source_ids": sorted(component),
                    **lineage,
                }
            )
        if split_required and admitted_component_rows:
            admitted_sources = {
                source_id
                for row in admitted_component_rows
                for source_id in row["source_ids"]
            }
            component_actions.append(
                {
                    "action": "split_disconnected_components",
                    "proposal_id": str(proposal.get("proposal_id") or ""),
                    "parent_source_ids": sorted(proposal_sources),
                    "admitted_components": admitted_component_rows,
                    "discarded_source_ids": sorted(proposal_sources - admitted_sources),
                }
            )

    deduplicated_candidates: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        signature = _stable_hash(
            {
                "core_source_ids": candidate["core_source_ids"],
                "proposition_ids": sorted(
                    str(row.get("proposition_id") or "")
                    for row in candidate["propositions"]
                ),
                "family_relations": candidate["component_lineage"]["family_relations"],
            }
        )
        incumbent = deduplicated_candidates.get(signature)
        if incumbent is None:
            deduplicated_candidates[signature] = candidate
            continue

        def ranking(row: Mapping[str, Any]) -> tuple[int, int, int, str]:
            return (
                -len(row["propositions"]),
                -len(row["family_relations"]),
                len(row["context_source_ids"]) + len(row["bridge_source_ids"]),
                str(row["semantic_identity"]),
            )

        winner, duplicate = sorted((incumbent, candidate), key=ranking)
        deduplicated_candidates[signature] = winner
        component_actions.append(
            {
                "action": "deduplicate_component",
                "component_signature": signature,
                "kept_proposal_id": str(winner["proposal"].get("proposal_id") or ""),
                "duplicate_proposal_id": str(
                    duplicate["proposal"].get("proposal_id") or ""
                ),
                "core_source_ids": winner["core_source_ids"],
            }
        )
    candidates = list(deduplicated_candidates.values())

    # A reasoner may return the same debate family more than once when the
    # same studies support several closely related propositions. Keep the
    # propositions separate, but project them as one cluster when the already
    # validated components have the identical core-source set and share a
    # bounded outcome. This creates no new relation edge and does not merge
    # families merely because their labels or topic words overlap.
    nondiscriminating_outcomes = {
        "agreement",
        "effect",
        "effectiveness",
        "stability",
        "success",
    }

    def exact_outcome_signature(candidate: Mapping[str, Any]) -> tuple[str, ...]:
        signatures = {
            tuple(
                sorted(
                    {
                        str(term).casefold()
                        for term in _as_mapping(relation.get("comparability")).get(
                            "shared_outcome_terms", []
                        )
                        or []
                        if str(term).casefold()
                        not in _BROAD_FIELD_TERMS
                        | _GENERIC_FAMILY_RELATION_TERMS
                        | nondiscriminating_outcomes
                    }
                )
            )
            for relation in candidate.get("family_relations", []) or []
        }
        signatures.discard(())
        # A candidate with multiple outcome signatures is not exact enough to
        # merge. This prevents A->B->C token-overlap chains from widening a
        # bounded family as the union grows.
        if len(signatures) != 1:
            return ()
        signature = next(iter(signatures))
        return signature if len(signature) >= 2 else ()

    parallel_groups: dict[
        tuple[tuple[str, ...], tuple[str, ...]], list[dict[str, Any]]
    ] = defaultdict(list)
    merged_candidates: list[dict[str, Any]] = []
    for candidate in candidates:
        signature = exact_outcome_signature(candidate)
        if not signature:
            merged_candidates.append(candidate)
            continue
        parallel_groups[(tuple(candidate["core_source_ids"]), signature)].append(
            candidate
        )

    for (core_source_ids, outcome_signature), family in sorted(parallel_groups.items()):
        if family:
            family_terms = set(outcome_signature)
            if len(family) == 1:
                merged_candidates.extend(family)
                continue

            def parallel_ranking(row: Mapping[str, Any]) -> tuple[int, int, int, str]:
                label = str(_as_mapping(row.get("proposal")).get("label") or "")
                return (
                    -len(_tokens(label) & family_terms),
                    -len(row.get("propositions", []) or []),
                    len(label),
                    str(row.get("semantic_identity") or ""),
                )

            winner = sorted(family, key=parallel_ranking)[0]
            propositions_by_id = {
                str(
                    proposition.get("proposition_id") or _stable_hash(proposition)
                ): dict(proposition)
                for candidate in family
                for proposition in candidate["propositions"]
            }
            relations_by_lineage = {
                _stable_hash(
                    {
                        "relation_type": relation.get("relation_type"),
                        "source_ids": sorted(relation.get("source_ids", []) or []),
                        "evidence": sorted(
                            (
                                str(reference.get("source_id") or ""),
                                str(
                                    reference.get("evidence_anchor_id")
                                    or reference.get("claim_id")
                                    or ""
                                ),
                            )
                            for reference in relation.get("evidence", []) or []
                        ),
                        "shared_outcome_terms": sorted(
                            _as_mapping(relation.get("comparability")).get(
                                "shared_outcome_terms", []
                            )
                            or []
                        ),
                    }
                ): dict(relation)
                for candidate in family
                for relation in candidate["family_relations"]
            }
            merged_propositions = sorted(
                propositions_by_id.values(),
                key=lambda row: str(row.get("proposition_id") or ""),
            )
            merged_relations = sorted(
                relations_by_lineage.values(),
                key=lambda row: (
                    str(row.get("relation_type") or ""),
                    tuple(str(value) for value in row.get("source_ids", []) or []),
                    str(row.get("rationale") or ""),
                ),
            )
            lineage = {
                "proposition_ids": [
                    str(row.get("proposition_id") or "") for row in merged_propositions
                ],
                "family_relations": [
                    {
                        "relation_type": str(row.get("relation_type") or ""),
                        "source_ids": sorted(
                            str(value) for value in row.get("source_ids", []) or []
                        ),
                        "evidence": sorted(
                            (
                                str(reference.get("source_id") or ""),
                                str(
                                    reference.get("evidence_anchor_id")
                                    or reference.get("claim_id")
                                    or ""
                                ),
                            )
                            for reference in row.get("evidence", []) or []
                        ),
                    }
                    for row in merged_relations
                ],
            }
            semantic_basis = [
                str(row.get("semantic_identity") or row.get("statement") or "")
                for row in merged_propositions
            ]
            merged_identity = _canonical_phrase(semantic_basis) or _stable_hash(lineage)
            merged_proposal = dict(winner["proposal"])
            merged_proposal.update(
                {
                    "proposal_id": f"proposal-merged-{_stable_hash(lineage)[:12]}",
                    "semantic_identity": merged_identity,
                    "formation_route": "merged_parallel_debate_family",
                    "parallel_candidate_merge": True,
                    "parent_proposal_ids": sorted(
                        str(candidate["proposal"].get("proposal_id") or "")
                        for candidate in family
                    ),
                    "coherence_rationale": (
                        "The same core studies address multiple located propositions about a shared bounded outcome. "
                        "The propositions remain distinct inside one debate-family cluster."
                    ),
                }
            )
            merged_candidates.append(
                {
                    **winner,
                    "proposal": merged_proposal,
                    "semantic_identity": merged_identity,
                    "component_lineage": lineage,
                    "propositions": merged_propositions,
                    "family_relations": merged_relations,
                    "context_source_ids": sorted(
                        {
                            source_id
                            for candidate in family
                            for source_id in candidate["context_source_ids"]
                        }
                    ),
                    "bridge_source_ids": sorted(
                        {
                            source_id
                            for candidate in family
                            for source_id in candidate["bridge_source_ids"]
                        }
                    ),
                }
            )
            component_actions.append(
                {
                    "action": "merge_parallel_components",
                    "core_source_ids": list(core_source_ids),
                    "parent_proposal_ids": merged_proposal["parent_proposal_ids"],
                    "proposition_ids": lineage["proposition_ids"],
                    "shared_outcome_terms": sorted(family_terms),
                }
            )
    candidates = merged_candidates

    # Cap only core analytical memberships. Context, bridge, and topic-
    # neighborhood membership do not consume the three-family core limit.
    selected_by_source: dict[str, set[str]] = defaultdict(set)
    for source_id in profile_by_source:
        available = [row for row in candidates if source_id in row["core_source_ids"]]
        available.sort(
            key=lambda row: (
                -row["core_evidence_base_count"],
                -row["core_family_count"],
                -len(row["propositions"]),
                row["semantic_identity"],
            )
        )
        selected_by_source[source_id] = {
            row["semantic_identity"] for row in available[:max_memberships]
        }

    relation_ids_by_source: dict[str, list[str]] = defaultdict(list)
    for relation in relations or []:
        for source_id in relation.get("source_ids", []) or []:
            relation_ids_by_source[str(source_id)].append(
                str(relation.get("relation_id") or "")
            )
    neighborhood_ids_by_source: dict[str, list[str]] = defaultdict(list)
    for neighborhood in topic_neighborhoods or []:
        for source_id in neighborhood.get("source_ids", []) or []:
            neighborhood_ids_by_source[str(source_id)].append(
                str(neighborhood.get("topic_neighborhood_id") or "")
            )

    clusters: list[dict[str, Any]] = []
    for candidate in candidates:
        included_core = [
            source_id
            for source_id in candidate["core_source_ids"]
            if candidate["semantic_identity"] in selected_by_source[source_id]
        ]
        included = sorted(
            set(included_core)
            | set(candidate["context_source_ids"])
            | set(candidate["bridge_source_ids"])
        )
        core_sources = sorted(included_core)
        core_families = sorted(
            {
                str(profile_by_source[source_id].get("study_family_id") or source_id)
                for source_id in core_sources
            }
        )
        core_evidence_bases = sorted(
            {
                evidence_base_id
                for source_id in core_sources
                if (
                    evidence_base_id := _profile_evidence_base_id(
                        profile_by_source[source_id]
                    )
                )
            }
        )
        family_relations = [
            {
                **dict(relation),
                "source_ids": sorted(
                    set(relation.get("source_ids", []) or []) & set(core_sources)
                ),
                "evidence": [
                    dict(reference)
                    for reference in relation.get("evidence", []) or []
                    if str(reference.get("source_id") or "") in set(core_sources)
                ],
            }
            for relation in candidate["family_relations"]
            if len(set(relation.get("source_ids", []) or []) & set(core_sources)) >= 2
        ]
        if (
            len(core_sources) < min_emerging
            or not _family_relation_graph_connected(set(core_sources), family_relations)
        ):
            rejected.append(
                {
                    "proposal_id": str(candidate["proposal"].get("proposal_id") or ""),
                    "semantic_identity": candidate["semantic_identity"],
                    "source_ids": included,
                    "action": "reject",
                    "reason": "overlap_policy_removed_family_coherence",
                }
            )
            continue
        cluster_propositions: list[dict[str, Any]] = []
        proposition_family_counts: list[int] = []
        for proposition in candidate["propositions"]:
            proposition_sources = {
                str(value)
                for value in proposition.get("source_ids", []) or []
                if str(value) in core_sources
            }
            proposition_families = sorted(
                {
                    str(
                        profile_by_source[source_id].get("study_family_id") or source_id
                    )
                    for source_id in proposition_sources
                }
            )
            proposition_evidence_bases = sorted(
                {
                    evidence_base_id
                    for source_id in proposition_sources
                    if (
                        evidence_base_id := _profile_evidence_base_id(
                            profile_by_source[source_id]
                        )
                    )
                }
            )
            if len(proposition_sources) < min_emerging:
                continue
            projected = dict(proposition)
            projected["source_ids"] = sorted(proposition_sources)
            projected["study_family_ids"] = proposition_families
            projected["independent_study_family_count"] = len(proposition_families)
            projected["evidence_base_group_ids"] = proposition_evidence_bases
            projected["effective_evidence_base_count"] = len(proposition_evidence_bases)
            projected["cells"] = [
                dict(cell)
                for cell in proposition.get("cells", []) or []
                if isinstance(cell, Mapping)
                and str(cell.get("source_id") or "") in proposition_sources
            ]
            projected["evidence"] = [
                dict(reference)
                for reference in proposition.get("evidence", []) or []
                if isinstance(reference, Mapping)
                and str(reference.get("source_id") or "") in proposition_sources
            ]
            cluster_propositions.append(projected)
            proposition_family_counts.append(len(proposition_evidence_bases))
        proposition_ids = sorted(row["proposition_id"] for row in cluster_propositions)
        cluster_id = (
            f"cluster-{slugify(candidate['semantic_identity'])}-"
            f"{_stable_hash({'semantic_identity': candidate['semantic_identity']})[:10]}"
        )
        strongest_proposition_family_count = max(proposition_family_counts, default=0)
        qualification = (
            "source_backed_cluster"
            if len(core_evidence_bases) >= min_backed
            else "emerging_cluster"
            if len(core_evidence_bases) >= min_emerging
            else "evidence_concentrated_cluster"
        )
        role_by_source = {
            source_id: (
                "core"
                if source_id in core_sources
                else "bridge"
                if source_id in candidate["bridge_source_ids"]
                else "context"
            )
            for source_id in included
        }
        revision_hash = _stable_hash(
            {
                "semantic_identity": candidate["semantic_identity"],
                "shared_question": str(
                    candidate["proposal"].get("shared_question") or ""
                ),
                "proposition_ids": proposition_ids,
                "source_roles": role_by_source,
                "family_relations": family_relations,
                "core_study_families": core_families,
                "core_evidence_bases": core_evidence_bases,
                "qualification": qualification,
            }
        )
        proposal = candidate["proposal"]
        label = str(
            proposal.get("label")
            or (
                cluster_propositions[0].get("statement") if cluster_propositions else ""
            )
            or "Analytical Cluster"
        )
        shared_question = str(
            proposal.get("shared_question")
            or (cluster_propositions[0].get("question") if cluster_propositions else "")
            or f"What does this collection establish about {label}?"
        )
        clusters.append(
            {
                "cluster_id": cluster_id,
                "semantic_identity": candidate["semantic_identity"],
                "label": label,
                "shared_question": shared_question,
                "bounded_object": str(
                    proposal.get("bounded_object") or candidate["semantic_identity"]
                ),
                "coherence_rationale": str(
                    proposal.get("coherence_rationale")
                    or "Located core-source findings address connected positions within one bounded research question."
                ),
                "proposal_id": str(proposal.get("proposal_id") or ""),
                "parallel_candidate_merge": bool(
                    proposal.get("parallel_candidate_merge", False)
                ),
                "parent_proposal_ids": sorted(
                    str(value)
                    for value in proposal.get("parent_proposal_ids", []) or []
                    if str(value)
                ),
                "proposal_supporting_evidence": [
                    reference
                    for row in [*cluster_propositions, *family_relations]
                    for reference in row.get("evidence", []) or []
                ],
                "formation_route": str(
                    proposal.get("formation_route") or "reasoner_debate_family"
                ),
                "mapping_mode": (
                    "comparative_proposition"
                    if cluster_propositions
                    else "thematic_subliterature"
                ),
                "proposition_ids": proposition_ids,
                "propositions": cluster_propositions,
                "family_relations": family_relations,
                "family_admission_passed": True,
                "source_roles": [
                    {"source_id": source_id, "role": role_by_source[source_id]}
                    for source_id in sorted(role_by_source)
                ],
                "core_source_ids": sorted(core_sources),
                "context_source_ids": sorted(
                    source_id
                    for source_id in included
                    if role_by_source[source_id] == "context"
                ),
                "bridge_source_ids": sorted(
                    source_id
                    for source_id in included
                    if role_by_source[source_id] == "bridge"
                ),
                "topic_neighborhood_ids": sorted(
                    {
                        value
                        for source_id in included
                        for value in neighborhood_ids_by_source[source_id]
                        if value
                    }
                ),
                "shared_concepts": [],
                "shared_normalized_tags": [],
                "shared_methods": [],
                "note_ids": sorted(
                    str(profile_by_source[source_id]["note_id"])
                    for source_id in included
                ),
                "source_ids": sorted(included),
                "study_family_ids": sorted(
                    {
                        str(
                            profile_by_source[source_id].get("study_family_id")
                            or source_id
                        )
                        for source_id in included
                    }
                ),
                "core_study_family_ids": core_families,
                "independent_study_family_count": len(core_families),
                "core_evidence_base_group_ids": core_evidence_bases,
                "effective_evidence_base_count": len(core_evidence_bases),
                "qualifying_proposition_family_count": strongest_proposition_family_count,
                "source_count": len(included),
                "status": qualification if auto_promote else "cluster_candidate",
                "qualification_status": qualification,
                "promoted": auto_promote,
                "automation_status": "promoted" if auto_promote else "candidate",
                "source_backed": len(core_evidence_bases) >= min_backed,
                "revision_hash": revision_hash,
                "relation_ids": sorted(
                    {
                        value
                        for source_id in included
                        for value in relation_ids_by_source[source_id]
                        if value
                    }
                ),
                "representative_sources": [
                    {
                        "note_id": profile_by_source[source_id]["note_id"],
                        "source_id": source_id,
                        "study_family_id": profile_by_source[source_id][
                            "study_family_id"
                        ],
                        "title": profile_by_source[source_id]["title"],
                        "note_path": profile_by_source[source_id]["note_path"],
                        "note_hash": profile_by_source[source_id]["note_hash"],
                        "cluster_role": role_by_source[source_id],
                    }
                    for source_id in sorted(included)
                ],
            }
        )

    clustered_sources = {
        source_id for cluster in clusters for source_id in cluster["source_ids"]
    }
    unclustered = []
    for profile in rows:
        source_id = str(profile["source_id"])
        if source_id in clustered_sources:
            continue
        if profile.get("limited"):
            reason = (
                profile.get("exclusion_reason")
                or "limited_profile_excluded_from_analytical_clustering"
            )
            reason_detail = "Limited coverage cannot support analytical cluster admission."
        else:
            reason, reason_detail = _specific_unclustered_reason(
                source_id, profile, rejected, topic_neighborhoods
            )
        unclustered.append(
            {
                "source_id": source_id,
                "note_id": profile["note_id"],
                "reason": reason,
                "reason_detail": reason_detail,
            }
        )
    return {
        "clusters": sorted(clusters, key=lambda row: row["cluster_id"]),
        "rejected_proposals": sorted(
            rejected, key=lambda row: (row["reason"], row["semantic_identity"])
        ),
        "component_actions": sorted(
            component_actions,
            key=lambda row: (
                str(row.get("action") or ""),
                str(row.get("proposal_id") or row.get("component_signature") or ""),
            ),
        ),
        "unclustered_sources": sorted(unclustered, key=lambda row: row["source_id"]),
        "max_cluster_memberships": max_memberships,
    }


def reconcile_cluster_registry(
    clusters: Sequence[Mapping[str, Any]],
    previous_registry: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Infer stable lifecycle events without changing semantic cluster IDs."""
    previous_payload = dict(previous_registry or {})
    previous = [dict(row) for row in previous_payload.get("clusters", []) or []]
    current = [dict(row) for row in clusters]
    old_by_identity: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in previous:
        identity = _canonical_phrase(
            row.get("semantic_identity") or row.get("label") or ""
        )
        if identity:
            old_by_identity[identity].append(row)
    for cluster in current:
        if any(
            str(row.get("cluster_id") or "") == str(cluster.get("cluster_id") or "")
            for row in previous
        ):
            continue
        identity = _canonical_phrase(
            cluster.get("semantic_identity") or cluster.get("label") or ""
        )
        matches = old_by_identity.get(identity, [])
        if len(matches) == 1 and matches[0].get("cluster_id"):
            cluster["cluster_id"] = str(matches[0]["cluster_id"])
    old_by_id = {
        str(row.get("cluster_id")): row for row in previous if row.get("cluster_id")
    }
    current_by_id = {str(row["cluster_id"]): row for row in current}
    ledger: list[dict[str, Any]] = [
        dict(row) for row in previous_payload.get("ledger", []) or []
    ]

    for cluster in current:
        old = old_by_id.get(str(cluster["cluster_id"]))
        if old is None:
            cluster["registry_status"] = "new"
            ledger.append(
                {
                    "event": "new",
                    "cluster_id": cluster["cluster_id"],
                    "revision_hash": cluster["revision_hash"],
                }
            )
        elif str(old.get("revision_hash")) == str(cluster.get("revision_hash")):
            cluster["registry_status"] = "unchanged"
            ledger.append(
                {
                    "event": "unchanged",
                    "cluster_id": cluster["cluster_id"],
                    "revision_hash": cluster["revision_hash"],
                }
            )
        else:
            cluster["registry_status"] = "revision"
            ledger.append(
                {
                    "event": "revision",
                    "cluster_id": cluster["cluster_id"],
                    "prior_revision_hash": str(old.get("revision_hash", "")),
                    "revision_hash": cluster["revision_hash"],
                    "added_source_ids": sorted(
                        set(cluster.get("source_ids", []))
                        - set(old.get("source_ids", []))
                    ),
                    "removed_source_ids": sorted(
                        set(old.get("source_ids", []))
                        - set(cluster.get("source_ids", []))
                    ),
                }
            )

    unmatched_old = [
        row for row in previous if str(row.get("cluster_id")) not in current_by_id
    ]
    unmatched_current = [
        row for row in current if str(row.get("cluster_id")) not in old_by_id
    ]
    old_to_new: dict[str, list[str]] = defaultdict(list)
    new_to_old: dict[str, list[str]] = defaultdict(list)
    for old in unmatched_old:
        old_sources = set(old.get("source_ids", []) or [])
        old_identity = _canonical_phrase(
            old.get("semantic_identity") or old.get("label")
        )
        for new in unmatched_current:
            membership_overlap = bool(
                old_sources & set(new.get("source_ids", []) or [])
            )
            semantic_overlap = bool(
                old_identity
                and old_identity == _canonical_phrase(new.get("semantic_identity"))
            )
            if membership_overlap or semantic_overlap:
                old_to_new[str(old.get("cluster_id"))].append(str(new["cluster_id"]))
                new_to_old[str(new["cluster_id"])].append(str(old.get("cluster_id")))

    retired: list[dict[str, Any]] = [
        dict(row) for row in previous_payload.get("retired_clusters", []) or []
    ]
    for old in unmatched_old:
        old_id = str(old.get("cluster_id"))
        successors = sorted(set(old_to_new.get(old_id, [])))
        if len(successors) > 1:
            ledger.append(
                {
                    "event": "split",
                    "prior_cluster_ids": [old_id],
                    "cluster_ids": successors,
                }
            )
        elif len(successors) == 1 and len(set(new_to_old.get(successors[0], []))) == 1:
            ledger.append(
                {
                    "event": "supersede",
                    "prior_cluster_ids": [old_id],
                    "cluster_ids": successors,
                }
            )
        elif not successors:
            retired.append({**old, "registry_status": "retired"})
            ledger.append(
                {"event": "retire", "prior_cluster_ids": [old_id], "cluster_ids": []}
            )
    for new_id, predecessors in sorted(new_to_old.items()):
        unique = sorted(set(predecessors))
        if len(unique) > 1:
            ledger.append(
                {"event": "merge", "prior_cluster_ids": unique, "cluster_ids": [new_id]}
            )

    def event_key(row: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
        return (
            str(row.get("event")),
            repr(row.get("cluster_ids", [])),
            repr(row.get("prior_cluster_ids", [])),
            str(row.get("cluster_id", "")),
            str(row.get("revision_hash", "")),
        )

    unique_ledger = {_stable_hash(row): row for row in ledger}
    unique_retired = {
        str(row.get("cluster_id")): row for row in retired if row.get("cluster_id")
    }
    return {
        "clusters": sorted(current, key=lambda row: row["cluster_id"]),
        "ledger": sorted(unique_ledger.values(), key=event_key),
        "retired_clusters": sorted(
            unique_retired.values(), key=lambda row: str(row.get("cluster_id"))
        ),
    }


def _evidence_ref(claim: Mapping[str, Any]) -> dict[str, Any]:
    anchor_id = str(claim.get("evidence_anchor_id") or claim.get("claim_id") or "")
    return {
        "evidence_anchor_id": anchor_id,
        "claim_id": anchor_id,
        "source_id": str(claim.get("source_id", "")),
        "study_family_id": str(claim.get("study_family_id", "")),
        "evidence_base_group_id": str(claim.get("evidence_base_group_id") or ""),
        "counted_as_independent": bool(claim.get("evidence_base_counted")),
        "independence_status": str(
            claim.get("independence_status") or "independence_uncertain"
        ),
        "locator": str(claim.get("locator", "")),
        "source_locator": dict(
            _as_mapping(claim.get("source_locator"))
            or _source_locator(claim.get("locator"))
        ),
        "support_status": str(claim.get("support_status") or "support_unknown"),
        "empirical_role": str(
            _as_mapping(claim.get("support_envelope")).get("empirical_role") or "none"
        ),
        "argument_role": str(
            _as_mapping(claim.get("support_envelope")).get("argument_role") or "none"
        ),
        "restrictions": [
            str(value)
            for value in _as_mapping(claim.get("support_envelope")).get(
                "restrictions", []
            )
            or []
            if str(value)
        ],
    }


def _resolve_reasoner_evidence(
    values: Any,
    profile_by_source: Mapping[str, Mapping[str, Any]],
    *,
    allowed_source_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    for value in values or []:
        if not isinstance(value, Mapping):
            continue
        reference = _as_mapping(value)
        source_id = str(reference.get("source_id") or "")
        if allowed_source_ids is not None and source_id not in allowed_source_ids:
            continue
        profile = profile_by_source.get(source_id)
        if profile is None or not _reference_matches_profile(reference, profile):
            continue
        claim_id = str(
            reference.get("evidence_anchor_id") or reference.get("claim_id") or ""
        )
        claim = next(
            row
            for row in profile.get("claims", []) or []
            if str(row.get("evidence_anchor_id") or row.get("claim_id") or "")
            == claim_id
        )
        resolved.append(_evidence_ref(claim))
    return sorted(
        {_stable_hash(row): row for row in resolved}.values(),
        key=lambda row: (row["source_id"], row["evidence_anchor_id"], row["locator"]),
    )


def _sanitize_reasoned_items(
    values: Any,
    profile_by_source: Mapping[str, Mapping[str, Any]],
    *,
    allowed_source_ids: set[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for value in values or []:
        if not isinstance(value, Mapping):
            continue
        item = _as_mapping(value)
        evidence = _resolve_reasoner_evidence(
            item.get("evidence") or item.get("supporting_evidence") or [],
            profile_by_source,
            allowed_source_ids=allowed_source_ids,
        )
        evidence = [
            reference
            for reference in evidence
            if str(reference.get("support_status") or "support_unknown")
            == "supported"
        ]
        if not evidence:
            continue
        cleaned = {
            str(key): val
            for key, val in item.items()
            if key not in {"evidence", "supporting_evidence"}
        }
        cleaned["evidence"] = evidence
        rows.append(cleaned)
    return rows


def _quantitative_text_errors(item: Mapping[str, Any]) -> list[str]:
    """Catch arithmetic mismatches before numerical prose reaches Markdown."""

    text = " ".join(
        str(item.get(key) or "")
        for key in (
            "assertion",
            "finding",
            "text",
            "summary",
            "position",
            "agreement",
            "contradiction",
            "technical_result",
            "technical_detail",
            "technical_context",
            "statistics",
            "plain_english_meaning",
            "plain_english",
        )
    )
    errors: list[str] = []
    percentage_matches = list(re.finditer(r"(?<![\d.])(\d+(?:\.\d+)?)\s*%", text))
    percentages = [
        float(match.group(1))
        for match in percentage_matches
        if not re.match(
            r"\s*(?:CI\b|confidence\s+interval\b)",
            text[match.end() : match.end() + 24],
            flags=re.I,
        )
    ]
    point_changes = [
        float(value)
        for value in re.findall(
            r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:percentage\s*points?|pp)\b",
            text,
            flags=re.I,
        )
    ]
    # Validate only numbers that the prose presents as one comparison. A long
    # source summary may contain several coefficients, probabilities, and
    # conditional effects; comparing every number with every other number
    # creates false arithmetic failures. Unpaired point estimates remain
    # source-local facts and are not silently forced into another comparison.
    comparison_triples: list[tuple[float, float, float]] = []
    for pattern, order in (
        (
            r"(\d+(?:\.\d+)?)\s*(?:percentage\s*points?|pp)\b[^.!?]{0,80}?"
            r"\bfrom\s+(?:about\s+|approximately\s+|roughly\s+)?(\d+(?:\.\d+)?)\s*%\s+to\s+"
            r"(?:about\s+|approximately\s+|roughly\s+)?(\d+(?:\.\d+)?)\s*%",
            "points_first",
        ),
        (
            r"\bfrom\s+(?:about\s+|approximately\s+|roughly\s+)?(\d+(?:\.\d+)?)\s*%\s+to\s+"
            r"(?:about\s+|approximately\s+|roughly\s+)?(\d+(?:\.\d+)?)\s*%"
            r"[^.!?]{0,80}?(\d+(?:\.\d+)?)\s*(?:percentage\s*points?|pp)\b",
            "percentages_first",
        ),
        (
            r"(\d+(?:\.\d+)?)\s*%\s*(?:versus|vs\.?)\s*(\d+(?:\.\d+)?)\s*%"
            r"[^.!?]{0,80}?(\d+(?:\.\d+)?)\s*(?:percentage\s*points?|pp)\b",
            "percentages_first",
        ),
    ):
        for match in re.finditer(pattern, text, flags=re.I):
            values = tuple(float(value) for value in match.groups())
            comparison_triples.append(
                values if order == "points_first" else (values[2], values[0], values[1])
            )
    if comparison_triples and any(
        abs(points - abs(right - left)) > 0.11
        for points, left, right in comparison_triples
    ):
        errors.append("percentage_point_arithmetic_mismatch")
    elif not comparison_triples and len(point_changes) == 1 and len(percentages) == 2:
        if abs(point_changes[0] - abs(percentages[1] - percentages[0])) > 0.11:
            errors.append("percentage_point_arithmetic_mismatch")

    # Convert decimals only when the text explicitly labels them as marginal
    # effects. Generic regression coefficients and p-values are not percentage
    # points and must not be compared with a probability change.
    decimal_effects = {
        float(value)
        for value in re.findall(
            r"\bmarginal\s+effects?\s*(?:is|was|=|of|:)?\s*([+-]?0\.\d{2,})(?!\d)",
            text,
            flags=re.I,
        )
    }
    if (
        decimal_effects
        and point_changes
        and not any(
            abs(abs(effect) * 100 - points) <= 0.11
            for effect in decimal_effects
            for points in point_changes
        )
    ):
        errors.append("decimal_effect_to_percentage_point_mismatch")
    return sorted(set(errors))


def _has_numerical_claim(text: Any) -> bool:
    return bool(
        re.search(
            r"(?:"
            r"(?<!\w)[+-]?(?:\d+(?:\.\d+)?|\.\d+)\s*(?:%|percentage\s*points?|pp\b|odds?\s*ratios?|coefficients?|CI\b)"
            r"|\b(?:odds?\s*ratios?|coefficients?|marginal\s+effects?|treatment\s+effects?|effect\s+sizes?|estimates?)"
            r"\b(?:\s+\w+){0,3}\s*(?:=|was|were|of|:)?\s*[+-]?(?:\d+(?:\.\d+)?|\.\d+)"
            r"|\bp\s*[<=>]\s*(?:\d+(?:\.\d+)?|\.\d+)"
            r"|\b[nN]\s*=\s*\d+"
            r"|\b\d+\s+(?:cases?|observations?|respondents?|participants?)\b"
            r"|\b\d+\s+(?:of|out\s+of)\s+\d+\b"
            r")",
            str(text or ""),
            flags=re.I,
        )
    )


_NUMBER_WORD_VALUES = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
    "thirteen": "13",
    "fourteen": "14",
    "fifteen": "15",
    "sixteen": "16",
    "seventeen": "17",
    "eighteen": "18",
    "nineteen": "19",
    "twenty": "20",
}

_NUMERIC_RELATION_GENERIC_TERMS = {
    "coefficient",
    "estimate",
    "model",
    "odds",
    "percent",
    "percentage",
    "probability",
    "ratio",
    "significant",
    "statistical",
    "variable",
}


def _numeric_evidence_tokens(value: Any) -> set[str]:
    """Return comparable numeric tokens while ignoring ordinary publication years."""

    text = str(value or "").casefold()
    values: set[str] = set()
    for raw in re.findall(
        r"(?<![\w.])[+-]?(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)",
        text,
    ):
        cleaned = raw.replace(",", "")
        number = float(cleaned)
        if number.is_integer() and 1900 <= abs(int(number)) <= 2100:
            continue
        values.add(f"{number:g}")
    values.update(
        normalized
        for word, normalized in _NUMBER_WORD_VALUES.items()
        if re.search(rf"\b{word}\b", text)
    )
    return values


def _reconcile_profile_anchors_with_atomic_notes(
    profiles: Sequence[dict[str, Any]],
    source_notes: Sequence[Mapping[str, Any]],
) -> int:
    """Quarantine numerical anchors whose term-value pairing is absent from the note.

    A profile may contain every number present in an atomic note while pairing a
    coefficient, count, or percentage with the wrong variable.  Locators alone
    do not catch that error.  This check compares the anchor with semantically
    plausible note clauses and requires one clause to contain every stated
    number.  It never repairs or invents a value; contradictory anchors become
    ``support_unknown``.
    """

    body_by_source = {
        str(row.get("source_id") or ""): source_note_semantic_components(
            str(row.get("body") or row.get("markdown") or "")
        )[1]
        for row in source_notes
        if row.get("source_id") and (row.get("body") or row.get("markdown"))
    }
    mismatch_count = 0
    for profile in profiles:
        source_id = str(profile.get("source_id") or "")
        body = body_by_source.get(source_id, "")
        if not body:
            continue
        # Keep decimal numbers and explanatory sentences intact.  Atomic-note
        # evidence is rendered as one finding per line, with semicolons used for
        # separate term-value pairings inside dense quantitative findings.
        fragments = [
            fragment.strip()
            for fragment in re.split(
                r"[\n;]+|(?<=[.!?])\s+(?=[A-Z#*_])|(?<=\))\s+(?=[A-Z#*_])",
                body,
            )
            if fragment.strip()
        ]
        fragment_terms = [_tokens(fragment) for fragment in fragments]
        note_term_frequency = Counter(
            term for terms in fragment_terms for term in set(terms)
        )
        for anchor in profile.get("claims", []) or []:
            if not isinstance(anchor, dict) or _anchor_is_composite_note_summary(anchor):
                continue
            initially_supported = (
                str(
                    _as_mapping(anchor.get("support_envelope")).get("support_status")
                    or anchor.get("support_status")
                    or "support_unknown"
                )
                == "supported"
            )
            # Machine quantitative-result records contain identifiers and scope
            # metadata whose digits are not part of the finding.  Prefer values
            # asserted in the finding text.  A separate magnitude is checked
            # only when it names a source-native statistic; derived plain-English
            # percentage-point conversions need not be verbatim in the note.
            finding_text = str(anchor.get("text") or "")
            stated_numbers = _numeric_evidence_tokens(finding_text)
            if len(finding_text) > 500 or len(stated_numbers) > 6:
                continue
            if not stated_numbers:
                magnitude = str(anchor.get("magnitude") or "")
                if re.search(
                    r"\b(?:coefficient|estimate|odds ratio|hazard ratio|lambda)\b|λ",
                    magnitude,
                    flags=re.I,
                ):
                    stated_numbers = _numeric_evidence_tokens(magnitude)
            if not stated_numbers:
                continue
            anchor_terms = (
                _tokens(
                    [
                        anchor.get("text"),
                        anchor.get("topic"),
                        anchor.get("plain_english_meaning"),
                    ]
                )
                - _GENERIC_FAMILY_RELATION_TERMS
                - _BROAD_FIELD_TERMS
                - _THEMATIC_OUTCOME_TERMS
                - _NUMERIC_RELATION_GENERIC_TERMS
            )
            anchor_terms = {
                term for term in anchor_terms if len(term) >= 3 and not term.isdigit()
            }
            ranked = sorted(
                (
                    (
                        len(anchor_terms & terms),
                        fragment,
                        anchor_terms & terms,
                    )
                    for fragment, terms in zip(fragments, fragment_terms, strict=True)
                ),
                key=lambda row: (row[0], len(row[1])),
                reverse=True,
            )
            plausible_fragments = [
                row
                for row in ranked
                if row[0] >= 2
                or (
                    row[0] == 1
                    and any(
                        len(term) >= 6 and note_term_frequency[term] <= 2
                        for term in row[2]
                    )
                )
            ]
            if not plausible_fragments:
                continue
            numbered_fragments = [
                (score, fragment, _numeric_evidence_tokens(fragment))
                for score, fragment, _ in plausible_fragments
                if _numeric_evidence_tokens(fragment)
            ]
            if not numbered_fragments or any(
                stated_numbers <= fragment_numbers
                for _, _, fragment_numbers in numbered_fragments
            ):
                continue
            envelope = dict(_as_mapping(anchor.get("support_envelope")))
            restrictions = list(envelope.get("restrictions", []) or [])
            warning = (
                "The committed atomic note does not confirm this numerical value "
                "for the stated variable or comparison."
            )
            if warning not in restrictions:
                restrictions.append(warning)
            envelope["restrictions"] = restrictions
            envelope["support_status"] = "support_unknown"
            anchor["support_envelope"] = envelope
            anchor["support_status"] = "support_unknown"
            anchor["note_numeric_pairing_valid"] = False
            source_locator = dict(_as_mapping(anchor.get("source_locator")))
            source_locator["supports_strong_assertion"] = False
            anchor["source_locator"] = source_locator
            if initially_supported:
                mismatch_count += 1
    return mismatch_count


def _quantitative_item_errors(
    item: Mapping[str, Any], *, require_comparable: bool = True
) -> list[str]:
    errors = list(_quantitative_text_errors(item))
    quantitative_text = " ".join(
        str(item.get(key) or "")
        for key in (
            "assertion",
            "finding",
            "text",
            "summary",
            "position",
            "agreement",
            "contradiction",
            "technical_result",
            "technical_detail",
            "technical_context",
            "statistics",
        )
    )
    has_numerical_claim = _has_numerical_claim(quantitative_text)
    comparisons = [
        _as_mapping(row)
        for row in item.get("quantitative_comparisons", []) or []
        if isinstance(row, Mapping)
    ]
    for comparison in comparisons if require_comparable else []:
        for field, error in (
            ("estimands_comparable", "quantitative_estimands_not_comparable"),
            ("outcomes_comparable", "quantitative_outcomes_not_comparable"),
            ("populations_comparable", "quantitative_populations_not_comparable"),
            ("arithmetic_reproducible", "quantitative_arithmetic_not_reproducible"),
        ):
            if comparison.get(field) is not True:
                errors.append(error)
    results = [
        _as_mapping(row)
        for row in item.get("quantitative_results", []) or []
        if isinstance(row, Mapping)
    ]
    if require_comparable and has_numerical_claim and not results and not comparisons:
        errors.append("quantitative_claim_missing_typed_results")
    if (
        require_comparable
        and has_numerical_claim
        and len(results) < 2
        and not comparisons
    ):
        errors.append("quantitative_comparison_requires_two_typed_results")
    if require_comparable and len(results) >= 2:
        for field, error in (
            ("estimand_type", "quantitative_estimands_not_comparable"),
            ("outcome_definition", "quantitative_outcomes_not_comparable"),
            ("population", "quantitative_populations_not_comparable"),
        ):
            values = {str(row.get(field) or "").strip().casefold() for row in results}
            if "" in values or len(values) != 1:
                errors.append(error)
    return sorted(set(errors))


def _evidence_quantitative_results(
    evidence: Sequence[Mapping[str, Any]],
    profile_by_source: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for reference in evidence:
        source_id = str(reference.get("source_id") or "")
        anchor_id = str(
            reference.get("evidence_anchor_id") or reference.get("claim_id") or ""
        )
        profile = profile_by_source.get(source_id, {})
        anchor = next(
            (
                row
                for row in profile.get("claims", []) or []
                if str(row.get("evidence_anchor_id") or row.get("claim_id") or "")
                == anchor_id
            ),
            {},
        )
        for result in anchor.get("quantitative_results", []) or []:
            if not isinstance(result, Mapping):
                continue
            result_row = _as_mapping(result)
            result_row.setdefault("source_id", source_id)
            result_row.setdefault("evidence_anchor_id", anchor_id)
            results.append(result_row)
    return sorted(
        {_stable_hash(row): row for row in results}.values(),
        key=lambda row: (
            str(row.get("source_id") or ""),
            str(row.get("quantitative_result_id") or ""),
        ),
    )


def _quantitative_comparison_records(
    syntheses: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for cluster_id, synthesis in sorted(syntheses.items()):
        persisted = [
            _as_mapping(row)
            for row in synthesis.get("quantitative_comparisons", []) or []
            if isinstance(row, Mapping)
        ]
        if persisted:
            rows.extend(persisted)
            continue
        for section in CLUSTER_SYNTHESIS_SECTIONS:
            for item in synthesis.get(section, []) or []:
                item_values = _as_mapping(item)
                errors = _quantitative_item_errors(item_values)
                if not errors and not any(
                    item_values.get(key)
                    for key in (
                        "statistics",
                        "technical_context",
                        "quantitative_results",
                        "quantitative_comparisons",
                    )
                ):
                    continue
                comparisons = [
                    _as_mapping(row)
                    for row in item_values.get("quantitative_comparisons", []) or []
                    if isinstance(row, Mapping)
                ]
                results = [
                    _as_mapping(row)
                    for row in item_values.get("quantitative_results", []) or []
                    if isinstance(row, Mapping)
                ]
                proposition_ids = [
                    str(value)
                    for value in item_values.get("proposition_ids", []) or []
                    if str(value)
                ]
                source_ids = sorted(
                    {
                        str(reference.get("source_id") or "")
                        for reference in item_values.get("evidence", []) or []
                        if reference.get("source_id")
                    }
                )
                rows.append(
                    {
                        "comparison_id": f"quantitative-comparison-{_stable_hash([cluster_id, section, item])[:16]}",
                        "proposition_id": proposition_ids[0]
                        if len(proposition_ids) == 1
                        else "",
                        "source_ids": source_ids,
                        "quantitative_result_ids": sorted(
                            str(row.get("quantitative_result_id") or "")
                            for row in results
                            if row.get("quantitative_result_id")
                        ),
                        "status": "rejected"
                        if errors
                        else (
                            "valid" if comparisons or len(results) >= 2 else "qualified"
                        ),
                        "estimands_comparable": not any(
                            "estimand" in error for error in errors
                        ),
                        "outcomes_comparable": not any(
                            "outcome" in error for error in errors
                        ),
                        "populations_comparable": not any(
                            "population" in error for error in errors
                        ),
                        "arithmetic_reproducible": not any(
                            "arithmetic" in error or "percentage_point" in error
                            for error in errors
                        ),
                        "reason": ";".join(errors)
                        if errors
                        else "deterministic_quantitative_checks_passed",
                        "qualifications": errors,
                    }
                )
    return rows


def _cluster_anchor_propositions(
    cluster: Mapping[str, Any],
) -> dict[tuple[str, str], list[str]]:
    by_anchor: dict[tuple[str, str], list[str]] = defaultdict(list)
    for proposition in cluster.get("propositions", []) or []:
        proposition_id = str(proposition.get("proposition_id") or "")
        for reference in proposition.get("evidence", []) or []:
            key = (
                str(reference.get("source_id") or ""),
                str(
                    reference.get("evidence_anchor_id")
                    or reference.get("claim_id")
                    or ""
                ),
            )
            if proposition_id and proposition_id not in by_anchor[key]:
                by_anchor[key].append(proposition_id)
    return by_anchor


def _cluster_theme_terms(cluster: Mapping[str, Any]) -> set[str]:
    return (
        _tokens(
            [
                cluster.get("label"),
                cluster.get("semantic_identity"),
                cluster.get("shared_question"),
                cluster.get("bounded_object"),
                *[
                    row.get("statement")
                    for row in cluster.get("propositions", []) or []
                    if isinstance(row, Mapping)
                ],
            ]
        )
        - _GENERIC_FAMILY_RELATION_TERMS
        - _BROAD_FIELD_TERMS
    )


def _cluster_display_term_sets(
    cluster: Mapping[str, Any],
) -> tuple[set[str], set[str]]:
    """Return weighted human-topic terms without scope-only outcome aliases."""

    primary = (
        _tokens(
            [
                cluster.get("display_label") or cluster.get("label"),
                cluster.get("display_question") or cluster.get("shared_question"),
            ]
        )
        - _GENERIC_FAMILY_RELATION_TERMS
        - _BROAD_FIELD_TERMS
    )
    secondary = (
        _tokens([cluster.get("semantic_identity"), cluster.get("bounded_object")])
        - _GENERIC_FAMILY_RELATION_TERMS
        - _BROAD_FIELD_TERMS
        - _THEMATIC_OUTCOME_TERMS
    )
    secondary = {value for value in secondary if not value.isdigit()}
    return primary, secondary


def _cluster_row_relevance(
    cluster: Mapping[str, Any], row: Mapping[str, Any]
) -> tuple[int, bool]:
    """Score whether a displayed finding actually answers the cluster question."""

    primary, secondary = _cluster_display_term_sets(cluster)
    row_terms = (
        _tokens(
            [
                row.get("title"),
                row.get("summary"),
                row.get("finding"),
                row.get("assertion"),
                row.get("plain_english_meaning"),
                row.get("technical_result"),
            ]
        )
        - _GENERIC_FAMILY_RELATION_TERMS
        - _BROAD_FIELD_TERMS
    )
    primary_overlap = len(primary & row_terms)
    secondary_overlap = len(secondary & row_terms)
    score = (primary_overlap * 3) + secondary_overlap
    return score, bool(primary_overlap or secondary_overlap >= 2)


def _cluster_display_source_roles(
    cluster: Mapping[str, Any], synthesis: Mapping[str, Any]
) -> dict[str, str]:
    """Return the canonical roles; projection must never silently reclassify sources."""

    del synthesis
    return {
        str(row.get("source_id") or ""): str(row.get("role") or "context")
        for row in cluster.get("source_roles", []) or []
        if isinstance(row, Mapping) and row.get("source_id")
    }


def _cluster_display_threads(
    cluster: Mapping[str, Any], synthesis: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Keep central intellectual threads and omit repeated map-fallback prose."""

    rows = [
        dict(row)
        for row in synthesis.get("evidence_threads", []) or []
        if isinstance(row, Mapping)
    ]
    non_map_rows = [
        row
        for row in rows
        if str(row.get("origin") or "") != "deterministic_source_contribution_map"
    ]
    relevant = [row for row in non_map_rows if _cluster_row_relevance(cluster, row)[1]]
    return relevant or [
        row
        for row in rows
        if str(row.get("origin") or "") == "deterministic_source_contribution_map"
    ]


def _cluster_display_question(
    cluster: Mapping[str, Any], synthesis: Mapping[str, Any]
) -> str:
    """Use a thematic question when the proposed causal question outruns membership."""

    question = _human_projection_text(
        cluster.get("display_question") or cluster.get("shared_question") or ""
    )
    untested_effectiveness_question = bool(
        not (cluster.get("propositions") or [])
        and re.search(
            r"\b(?:improve|increase|reduce|cause|effect|effective|effectiveness)\b",
            question,
            flags=re.I,
        )
    )
    if untested_effectiveness_question or not question:
        label = _human_projection_text(
            cluster.get("display_label")
            or cluster.get("label")
            or "this literature"
        )
        return f"What does this collection show about {label}?"
    return question


def _cluster_researcher_status(cluster: Mapping[str, Any]) -> str:
    """Translate admission strength into one useful, non-technical label."""

    status = str(
        cluster.get("qualification_status") or cluster.get("status") or ""
    )
    evidence_character = str(cluster.get("evidence_character") or "")
    propositions = list(cluster.get("propositions", []) or [])
    if evidence_character == "practitioner_guidance":
        return "Practitioner literature cluster"
    if status == "source_backed_cluster" and not propositions:
        return "Established thematic cluster"
    if status == "emerging_cluster" and not propositions:
        return "Emerging thematic cluster"
    return {
        "source_backed_cluster": "Established multi-study cluster",
        "emerging_cluster": "Emerging cluster",
        "evidence_concentrated_cluster": "Thematic cluster with limited independent evidence",
    }.get(status, "Thematic cluster")


def _apply_researcher_display_safeguards(
    clusters: Sequence[Mapping[str, Any]],
    profiles: Sequence[Mapping[str, Any]],
) -> None:
    """Correct human labels when proposal wording outruns source scope or type.

    Stable semantic IDs and membership remain unchanged. These fields govern
    only the researcher-facing title, question, and boundary explanation.
    """

    profile_by_source = {str(row.get("source_id") or ""): row for row in profiles}
    for raw_cluster in clusters:
        if not isinstance(raw_cluster, dict):
            continue
        cluster = raw_cluster
        label = str(cluster.get("label") or "").strip()
        question = str(cluster.get("shared_question") or "").strip()
        if re.fullmatch(r"Mediation Success Determinants", label, re.I):
            cluster["display_label"] = "Determinants of Mediation Success"
        core_profiles = [
            profile_by_source.get(str(role.get("source_id") or ""), {})
            for role in cluster.get("source_roles", []) or []
            if isinstance(role, Mapping) and str(role.get("role") or "") == "core"
        ]
        core_texts = [
            " ".join(
                _flatten_values(
                    [
                        _as_mapping(profile.get("context")).get("title"),
                        profile.get("title"),
                        profile.get("cases"),
                        profile.get("populations"),
                        profile.get("research_questions"),
                    ]
                )
            ).casefold()
            for profile in core_profiles
        ]
        label_parts = re.split(r"\s+and\s+", label, maxsplit=1, flags=re.I)
        if len(label_parts) == 2:
            question_tail = re.split(r"\s+and\s+", question, maxsplit=1, flags=re.I)
            promised_terms = _comparability_tokens(
                [label_parts[1], question_tail[1] if len(question_tail) == 2 else ""]
            )
            located_terms = _comparability_tokens(
                [
                    anchor.get("text") or anchor.get("claim") or ""
                    for profile in core_profiles
                    for anchor in profile.get("claims", []) or []
                    if _anchor_is_synthesis_eligible(anchor)
                ]
            )
            if promised_terms and not (promised_terms & located_terms):
                narrowed_label = label_parts[0].strip(" ,;:-")
                cluster["display_label"] = narrowed_label
                cluster["display_question"] = (
                    f"What major patterns and findings does this collection identify about {narrowed_label.lower()}?"
                )
                cluster["display_scope_note"] = (
                    "The display title is narrower than the proposal because the admitted anchors do not "
                    f"substantiate its additional emphasis on {label_parts[1].strip()}."
                )

        restrictive_civil_scope = bool(
            re.search(r"\b(?:civil wars?|civil conflicts?)\b", f"{label} {question}", re.I)
        )
        wider_conflict_scopes = sorted(
            {
                scope
                for text in core_texts
                for pattern, scope in (
                    (r"\binternational disputes?\b", "international disputes"),
                    (r"\binternational crises?\b", "international crises"),
                    (r"\binterstate conflicts?\b", "interstate conflicts"),
                    (
                        r"\binternationalized ethnic conflicts?\b",
                        "internationalized ethnic conflicts",
                    ),
                )
                if re.search(pattern, text, re.I)
            }
        )
        if restrictive_civil_scope and len(wider_conflict_scopes) >= 2:
            broadened = re.sub(
                r"\s+in\s+(?:civil wars?|civil conflicts?)\b",
                "",
                label,
                flags=re.I,
            ).strip()
            if re.fullmatch(r"Mediation Success Determinants", broadened, re.I):
                broadened = "Determinants of Mediation Success"
            cluster["display_label"] = broadened or label
            cluster["display_question"] = re.sub(
                r"\s+in\s+(?:civil wars?|civil conflicts?)\b",
                "",
                question,
                flags=re.I,
            ).strip()
            cluster["display_scope_note"] = (
                "The title is intentionally broader than the proposal label because the core evidence also covers "
                + ", ".join(wider_conflict_scopes)
                + "."
            )
            cluster["removed_restrictive_scope_terms"] = ["civil war", "civil wars"]

        source_kinds = [
            " ".join(
                _flatten_values(
                    [
                        profile.get("source_role"),
                        profile.get("methods"),
                        _as_mapping(profile.get("context")).get("title"),
                        profile.get("title"),
                    ]
                )
            ).casefold()
            for profile in core_profiles
        ]
        conference_series = bool(source_kinds) and all(
            re.search(r"\b(?:conference|proceedings)\b", value)
            for value in source_kinds
        )
        if conference_series:
            base = str(cluster.get("bounded_object") or label)
            base = re.sub(
                r"^\s*conference reports? from (?:the )?", "", base, flags=re.I
            )
            base = re.sub(r"\bseries\b", "", base, flags=re.I)
            base = re.sub(r"\b(?:insights?|findings?)\b", "", base, flags=re.I)
            base = re.sub(r"\s+", " ", base).strip(" -:")
            if re.search(r"\bconference$", base, re.I):
                base += "s"
            cluster["display_label"] = f"Practitioner Priorities from the {base}"
            cluster["display_question"] = (
                "Which challenges and priorities do these conference reports identify for mediation practice?"
            )
            cluster["evidence_character"] = "practitioner_guidance"


def _cluster_display_coherence(
    cluster: Mapping[str, Any], synthesis: Mapping[str, Any]
) -> str:
    """Describe admitted thematic fit without turning grouping prose into evidence."""

    rationale = _human_projection_text(
        synthesis.get("coherence_rationale")
        or cluster.get("coherence_rationale")
        or ""
    )
    if rationale and not _has_unqualified_causal_language(rationale):
        return rationale
    question = _cluster_display_question(cluster, synthesis)
    return (
        f"These sources are grouped because they centrally address the bounded question: {question} "
        "Their contributions may answer different parts of that question; cluster membership alone "
        "does not imply agreement or establish a shared causal effect."
    )


def _contribution_display_is_complete(row: Mapping[str, Any]) -> bool:
    text = str(row.get("finding") or "").strip()
    return bool(text) and not bool(
        re.search(
            r"(?:\.\.\.|\b(?:and|or|versus|compared with|compared to))\s*$", text, re.I
        )
    )


def _anchor_matches_cluster_theme(
    anchor: Mapping[str, Any],
    cluster_terms: set[str],
) -> bool:
    anchor_terms = (
        _tokens(
            [
                anchor.get("text"),
                anchor.get("claim"),
                anchor.get("plain_english_meaning"),
                anchor.get("topic"),
                anchor.get("dimensions"),
                _as_mapping(anchor.get("support_envelope")).get("scope"),
            ]
        )
        - _GENERIC_FAMILY_RELATION_TERMS
    )
    return bool(anchor_terms & cluster_terms) or (
        bool(anchor_terms & _THEMATIC_OUTCOME_TERMS)
        and bool(cluster_terms & _THEMATIC_OUTCOME_TERMS)
    )


def _cluster_membership_anchor_keys(
    cluster: Mapping[str, Any],
) -> set[tuple[str, str]]:
    return {
        (
            str(reference.get("source_id") or ""),
            str(reference.get("evidence_anchor_id") or reference.get("claim_id") or ""),
        )
        for relation in cluster.get("family_relations", []) or []
        if isinstance(relation, Mapping)
        for reference in relation.get("evidence", []) or []
        if isinstance(reference, Mapping)
    }


def _anchor_is_composite_note_summary(anchor: Mapping[str, Any]) -> bool:
    """Identify legacy rows that bundle several independently located findings."""

    text = str(anchor.get("text") or anchor.get("claim") or "").strip()
    source_locators = [
        row
        for row in anchor.get("source_locators", []) or []
        if isinstance(row, Mapping) and row.get("value")
    ]
    raw_locators = [
        value for value in _flatten_values(anchor.get("locators", [])) if value
    ]
    locator_text = str(anchor.get("locator") or "")
    locator_count = max(
        len(source_locators),
        len(raw_locators),
        len([part for part in re.split(r"\s*;\s*", locator_text) if part]),
    )
    sentence_count = len(
        [sentence for sentence in re.split(r"(?<=[.!?])\s+", text) if sentence]
    )
    return bool(
        (locator_count >= 3 and (len(text) >= 450 or sentence_count >= 3))
        or len(text) >= 900
    )


def _universal_direction_conflicts_with_evidence(
    statement: Any,
    evidence: Sequence[Mapping[str, Any]],
    anchor_by_key: Mapping[tuple[str, str], Mapping[str, Any]],
) -> bool:
    """Reject a universal directional summary when a cited source points elsewhere."""

    text = _human_projection_text(statement)
    if not re.search(r"\b(?:all|both|each|every|consistently)\b", text, re.I):
        return False
    upward = bool(
        re.search(
            r"\b(?:ris(?:e|es|ing|en)|rose|increas(?:e|es|ed|ing)|grew|growth|higher|more)\b",
            text,
            re.I,
        )
    )
    downward = bool(
        re.search(
            r"\b(?:declin(?:e|es|ed|ing)|decreas(?:e|es|ed|ing)|fell|fall(?:s|ing)?|halv(?:e|ed)|lower|fewer)\b",
            text,
            re.I,
        )
    )
    if upward == downward:
        return False
    anchor_texts = [
        str(anchor.get("text") or anchor.get("claim") or "")
        for reference in evidence
        if (
            anchor := anchor_by_key.get(
                (
                    str(reference.get("source_id") or ""),
                    str(
                        reference.get("evidence_anchor_id")
                        or reference.get("claim_id")
                        or ""
                    ),
                )
            )
        )
    ]
    if upward:
        return any(
            re.search(
                r"\b(?:declin(?:e|es|ed|ing)|decreas(?:e|es|ed|ing)|fell|halv(?:e|ed)|lower|fewer)\b",
                value,
                re.I,
            )
            for value in anchor_texts
        )
    return any(
        re.search(
            r"\b(?:ris(?:e|es|ing|en)|rose|increas(?:e|es|ed|ing)|grew|growth|higher|more)\b",
            value,
            re.I,
        )
        for value in anchor_texts
    )


def _universal_claim_missing_core_coverage(
    statement: Any,
    evidence: Sequence[Mapping[str, Any]],
    core_source_ids: set[str],
) -> bool:
    """Universal summaries require evidence from every core study they describe."""

    text = _human_projection_text(statement)
    if not re.search(
        r"\b(?:all|both|each|every|consistently)\b|"
        r"\bthe literature\s+(?:agrees|shows|finds|reports|establishes|demonstrates)\b",
        text,
        re.I,
    ):
        return False
    if len(core_source_ids) < 2:
        return False
    evidence_source_ids = {
        str(reference.get("source_id") or "")
        for reference in evidence
        if str(reference.get("source_id") or "")
    }
    return not core_source_ids.issubset(evidence_source_ids)


def _source_attribution_aliases(
    profile: Mapping[str, Any], source_id: str
) -> set[str]:
    """Return conservative author or institution names safe for prose matching."""

    aliases: set[str] = set()
    context = _as_mapping(profile.get("context"))
    for value in (profile.get("citation_key"), context.get("citation_key")):
        compact = re.sub(r"\d{4}[a-z]?$", "", str(value or ""), flags=re.I)
        compact = re.sub(r"[^A-Za-z][A-Za-z]*$", "", compact).strip()
        if len(compact) >= 5:
            aliases.add(compact)
    lineage = _as_mapping(profile.get("study_lineage"))
    for author in lineage.get("authors", []) or []:
        parts = re.findall(r"[A-Za-z][A-Za-z'’-]+", str(author))
        if parts and len(parts[-1]) >= 5:
            aliases.add(parts[-1])
    for institution in lineage.get("institutions", []) or []:
        label = str(institution or "").strip()
        if len(label) >= 5:
            aliases.add(label)
    label = _source_attribution_label(profile, source_id)
    compact_label = re.sub(r"\d{4}[a-z]?$", "", label, flags=re.I).strip(" -–—")
    if re.fullmatch(r"[A-Za-z][A-Za-z'’-]{4,}", compact_label):
        aliases.add(compact_label)
    return aliases


def _named_attribution_missing_source_ids(
    statement: Any,
    evidence: Sequence[Mapping[str, Any]],
    profile_by_source: Mapping[str, Mapping[str, Any]],
    allowed_source_ids: set[str],
) -> list[str]:
    """Detect prose that names one mapped source but cites another."""

    text = _human_projection_text(statement)
    evidence_source_ids = {
        str(reference.get("source_id") or "")
        for reference in evidence
        if str(reference.get("source_id") or "")
    }
    missing: list[str] = []
    for source_id in sorted(allowed_source_ids):
        aliases = _source_attribution_aliases(
            profile_by_source.get(source_id, {}), source_id
        )
        if any(
            re.search(rf"(?<![A-Za-z]){re.escape(alias)}(?![A-Za-z])", text, re.I)
            for alias in aliases
        ) and source_id not in evidence_source_ids:
            missing.append(source_id)
    return missing


def _anchor_technical_result(anchor: Mapping[str, Any]) -> str:
    return "; ".join(
        value
        for value in (
            str(anchor.get("magnitude") or ""),
            str(anchor.get("comparison") or ""),
            (
                str(anchor.get("uncertainty") or "")
                if re.search(
                    r"\d|confidence interval|standard error|credible interval|\bp\s*[<=>]",
                    str(anchor.get("uncertainty") or ""),
                    flags=re.I,
                )
                else ""
            ),
        )
        if value and value.casefold() not in {"not_reported", "not reported"}
    )


_LOW_VALUE_ACTIVITY_RESULT = re.compile(
    r"\b(?:trained|training|workshops?|participants?|attendees?|journalists?|"
    r"website visits?|page views?|downloads?|social media|people reached|beneficiaries)\b",
    flags=re.I,
)


def _contribution_substantive_penalty(
    cluster: Mapping[str, Any], row: Mapping[str, Any]
) -> int:
    """Prefer analytical findings over implementation-volume statistics.

    Activity counts remain valid evidence and are retained, but they should not
    displace a source's central mechanism, outcome, or argument unless the
    cluster itself is specifically about training, reach, or implementation.
    """

    finding = " ".join(
        _flatten_values(
            [
                row.get("finding"),
                row.get("text"),
                row.get("claim"),
                row.get("plain_english_meaning"),
            ]
        )
    )
    if not _LOW_VALUE_ACTIVITY_RESULT.search(finding):
        return 0
    cluster_scope = " ".join(
        _flatten_values(
            [
                cluster.get("label"),
                cluster.get("display_label"),
                cluster.get("shared_question"),
                cluster.get("display_question"),
                cluster.get("bounded_object"),
            ]
        )
    )
    return 0 if _LOW_VALUE_ACTIVITY_RESULT.search(cluster_scope) else 1


def _fallback_source_contributions(
    cluster: Mapping[str, Any],
    profiles: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Keep each study's important cluster-relevant finding visible without calling it agreement."""

    profile_by_source = {str(row.get("source_id") or ""): row for row in profiles}
    role_by_source = {
        str(row.get("source_id") or ""): str(row.get("role") or "context")
        for row in cluster.get("source_roles", []) or []
        if isinstance(row, Mapping)
    }
    proposition_ids_by_anchor = _cluster_anchor_propositions(cluster)
    cluster_terms = _cluster_theme_terms(cluster)
    membership_anchor_keys = _cluster_membership_anchor_keys(cluster)
    contributions: list[dict[str, Any]] = []
    for source_id in cluster.get("source_ids", []) or []:
        source_id = str(source_id)
        profile = profile_by_source.get(source_id)
        if profile is None:
            continue
        role = role_by_source.get(source_id, "context")
        anchors = [
            row
            for row in profile.get("claims", []) or []
            if _anchor_is_synthesis_eligible(row)
            and (
                (
                    source_id,
                    str(row.get("evidence_anchor_id") or row.get("claim_id") or ""),
                )
                in membership_anchor_keys
                or bool(
                    proposition_ids_by_anchor.get(
                        (
                            source_id,
                            str(
                                row.get("evidence_anchor_id")
                                or row.get("claim_id")
                                or ""
                            ),
                        )
                    )
                )
                or _anchor_matches_cluster_theme(row, cluster_terms)
            )
        ]
        specific_anchors = [
            row for row in anchors if not _anchor_is_composite_note_summary(row)
        ]
        if specific_anchors:
            anchors = specific_anchors
        anchors.sort(
            key=lambda row: (
                not bool(
                    proposition_ids_by_anchor.get(
                        (
                            source_id,
                            str(
                                row.get("evidence_anchor_id")
                                or row.get("claim_id")
                                or ""
                            ),
                        )
                    )
                ),
                _contribution_substantive_penalty(cluster, row),
                -len(
                    cluster_terms
                    & _tokens(
                        [row.get("text"), row.get("topic"), row.get("dimensions")]
                    )
                ),
                str(row.get("evidence_anchor_id") or row.get("claim_id") or ""),
            )
        )
        selected = anchors[:3] if role == "core" else anchors[:1]
        for anchor in selected:
            anchor_id = str(
                anchor.get("evidence_anchor_id") or anchor.get("claim_id") or ""
            )
            proposition_ids = proposition_ids_by_anchor.get((source_id, anchor_id), [])
            if role == "context":
                comparison_status = "context_only"
            elif role == "bridge":
                comparison_status = "context_only"
            else:
                comparison_status = "single_source"
            contribution_kind = (
                "direct_proposition_finding"
                if proposition_ids
                else "bridge_evidence"
                if role == "bridge"
                else "conceptual_context"
                if role == "context"
                else "unique_cluster_relevant_finding"
            )
            contribution_id = f"contribution-{_stable_hash([cluster.get('cluster_id'), source_id, anchor_id])[:16]}"
            finding = str(anchor.get("text") or "")
            plain_english_meaning = str(anchor.get("plain_english_meaning") or "")
            if (
                _has_unqualified_causal_language(finding)
                and not _anchor_supports_causal_claim(anchor)
            ):
                boundary = _causal_support_boundary(
                    _as_mapping(anchor.get("support_envelope")).get(
                        "restrictions", []
                    )
                    or []
                )
                finding = _narrow_noncausal_organizational_language(
                    finding, boundary=boundary
                )
                plain_english_meaning = (
                    _narrow_noncausal_organizational_language(
                        plain_english_meaning, boundary=boundary
                    )
                    if plain_english_meaning
                    else "This evidence does not by itself establish causation."
                )
            contributions.append(
                {
                    "contribution_id": contribution_id,
                    "source_id": source_id,
                    "cluster_role": role,
                    "contribution_kind": contribution_kind,
                    "finding": finding,
                    "plain_english_meaning": plain_english_meaning,
                    "technical_result": _anchor_technical_result(anchor),
                    "comparison_status": comparison_status,
                    "evidence_thread_id": "",
                    "related_proposition_ids": proposition_ids,
                    "relation_to_cluster_question": (
                        "Direct evidence for an admitted proposition."
                        if proposition_ids
                        else "A cluster-relevant source finding retained without a cross-source comparison."
                    ),
                    "evidence": [_evidence_ref(anchor)],
                }
            )
    return contributions


def _source_attribution_label(profile: Mapping[str, Any], source_id: str) -> str:
    """Return a compact human label for deterministic source attribution."""

    context = _as_mapping(profile.get("context"))
    for value in (
        profile.get("citation_key"),
        context.get("citation_key"),
    ):
        label = _human_projection_text(value or "")
        if label:
            return label
    note_path = str(profile.get("note_path") or context.get("note_path") or "")
    if note_path:
        stem = Path(note_path).stem
        citation_prefix = stem.split(" - ", 1)[0].strip()
        if citation_prefix:
            return _human_projection_text(citation_prefix)
    title = _human_projection_text(profile.get("title") or context.get("title") or "")
    if title:
        words = title.split()
        return " ".join(words[:8]) + ("…" if len(words) > 8 else "")
    return source_id


def _source_contribution_map_thread(
    cluster: Mapping[str, Any],
    contributions: Sequence[Mapping[str, Any]],
    profile_by_source: Mapping[str, Mapping[str, Any]],
    cluster_role_by_source: Mapping[str, str],
) -> dict[str, Any] | None:
    """Build a readable non-comparative answer from validated source findings."""

    first_core_contribution: dict[str, Mapping[str, Any]] = {}
    for contribution in contributions:
        source_id = str(contribution.get("source_id") or "")
        finding = _human_projection_text(contribution.get("finding") or "")
        if (
            not source_id
            or not finding
            or cluster_role_by_source.get(source_id, "context") != "core"
            or source_id in first_core_contribution
        ):
            continue
        first_core_contribution[source_id] = contribution
    ordered = [
        first_core_contribution[source_id]
        for source_id in (str(value) for value in cluster.get("source_ids", []) or [])
        if source_id in first_core_contribution
    ]
    if len(ordered) < 2:
        return None

    displayed = ordered[:4]
    source_findings: list[str] = []
    evidence: list[dict[str, Any]] = []
    for contribution in displayed:
        source_id = str(contribution.get("source_id") or "")
        label = _source_attribution_label(
            profile_by_source.get(source_id, {}), source_id
        )
        finding = _human_projection_text(contribution.get("finding") or "")
        first_sentence = re.split(r"(?<=[.!?])\s+", finding, maxsplit=1)[0].rstrip(" .")
        source_findings.append(f"{label}: {first_sentence}")
        evidence.extend(
            dict(reference)
            for reference in contribution.get("evidence", []) or []
            if isinstance(reference, Mapping)
        )
    question = _human_projection_text(
        cluster.get("display_question")
        or cluster.get("shared_question")
        or cluster.get("display_label")
        or cluster.get("label")
        or "this cluster question"
    )
    summary = (
        f"For “{question}”, the located evidence divides into distinct contributions: "
        + "; ".join(source_findings)
        + ". These findings explain why the sources belong in one literature cluster, "
        "but their different questions and evidence types do not make them independent "
        "tests of one common result."
    )
    evidence = list(
        {
            (
                str(reference.get("source_id") or ""),
                str(
                    reference.get("evidence_anchor_id")
                    or reference.get("claim_id")
                    or ""
                ),
                str(reference.get("locator") or ""),
            ): reference
            for reference in evidence
        }.values()
    )
    thread_id = (
        "thread-source-specific-"
        + _stable_hash(
            [
                cluster.get("cluster_id"),
                [
                    (
                        str(row.get("source_id") or ""),
                        str(row.get("contribution_id") or ""),
                    )
                    for row in ordered
                ],
            ]
        )[:16]
    )
    return {
        "thread_id": thread_id,
        "assertion_id": f"assertion-{thread_id}",
        "item_id": f"assertion-{thread_id}",
        "title": "Distinct source contributions to the cluster question",
        "summary": summary,
        "plain_english_meaning": (
            "The publications illuminate the same bounded research problem. Their findings remain "
            "source-specific unless a proposition-level comparison passes the stricter gate."
        ),
        "relationship": "source_specific_map",
        "source_ids": [str(row.get("source_id") or "") for row in ordered],
        "proposition_ids": sorted(
            {
                str(proposition_id)
                for row in ordered
                for proposition_id in row.get("related_proposition_ids", []) or []
                if str(proposition_id)
            }
        ),
        "family_relation_types": [],
        "origin": "deterministic_source_contribution_map",
        "qualification": "source_specific_only",
        "evidence": evidence,
    }


def _cluster_synthesis_quality_errors(
    synthesis: Mapping[str, Any],
    cluster: Mapping[str, Any],
) -> list[str]:
    """Require a real verdict and complete source-specific finding coverage."""

    errors: list[str] = []
    verdict = _human_projection_text(synthesis.get("synthesis") or "")
    verdict_words = re.findall(r"\b[\w'-]+\b", verdict)
    verdict_sentences = [
        value.strip() for value in re.split(r"(?<=[.!?])\s+", verdict) if value.strip()
    ]
    if not verdict:
        errors.append("missing_substantive_verdict")
    elif len(verdict_words) < MIN_CLUSTER_VERDICT_WORDS or len(verdict_sentences) < 2:
        errors.append("verdict_too_thin")

    if not synthesis.get("central_findings") and not synthesis.get("evidence_threads"):
        errors.append("missing_evidence_threads_or_central_findings")

    admitted_propositions = {
        str(row.get("proposition_id") or ""): row
        for row in cluster.get("propositions", []) or []
        if str(row.get("proposition_id") or "")
    }
    admitted_proposition_ids = {
        str(value)
        for value in (cluster.get("proposition_ids") or admitted_propositions)
        if str(value)
    }
    synthesis_required_proposition_ids = {
        proposition_id
        for proposition_id in admitted_proposition_ids
        if proposition_id not in admitted_propositions
        or admitted_propositions[proposition_id].get("effective_evidence_base_count")
        is None
        or int(
            admitted_propositions[proposition_id].get(
                "effective_evidence_base_count", 0
            )
            or 0
        )
        >= 2
    }
    covered_proposition_ids = {
        str(value)
        for row in synthesis.get("synthesis_assertions", []) or []
        for value in row.get("proposition_ids", []) or []
        if str(value)
    }
    for proposition_id in sorted(
        synthesis_required_proposition_ids - covered_proposition_ids
    ):
        errors.append(f"uncovered_admitted_proposition:{proposition_id}")

    role_by_source = {
        str(row.get("source_id") or ""): str(row.get("role") or "")
        for row in cluster.get("source_roles", []) or []
        if isinstance(row, Mapping) and row.get("source_id")
    }
    core_source_ids = {
        source_id for source_id, role in role_by_source.items() if role == "core"
    }
    if not core_source_ids:
        core_source_ids = {
            str(reference.get("source_id") or "")
            for proposition in cluster.get("propositions", []) or []
            for reference in proposition.get("evidence", []) or []
            if reference.get("source_id")
        }
    covered_source_ids = {
        str(reference.get("source_id") or "")
        for reference in synthesis.get("supporting_evidence", []) or []
        if reference.get("source_id")
    }
    for source_id in sorted(core_source_ids - covered_source_ids):
        errors.append(f"uncovered_core_source:{source_id}")

    contribution_source_ids = {
        str(row.get("source_id") or "")
        for row in synthesis.get("source_contributions", []) or []
        if row.get("source_id")
    }
    for source_id in sorted(core_source_ids - contribution_source_ids):
        errors.append(f"missing_core_source_contribution:{source_id}")

    if str(synthesis.get("debate_state") or "") not in DEBATE_STATES:
        errors.append("missing_relationship_assessment")

    limits_are_explicit = bool(
        synthesis.get("boundaries")
        or synthesis.get("boundary_conditions")
        or synthesis.get("methodological_fault_lines")
        or re.search(
            r"\b(?:limit(?:ation|ations|ed)?|uncertain|cannot|can't|does not|do not|"
            r"no causal|associational|descriptive|unsubstantiated|unsupported|scope)\b",
            verdict,
            flags=re.I,
        )
    )
    if not limits_are_explicit:
        errors.append("missing_evidentiary_limits")
    synthesis_level_prose = {
        "synthesis": synthesis.get("synthesis"),
        "coherence_rationale": synthesis.get("coherence_rationale"),
        "cluster_coherence_rationale": cluster.get("coherence_rationale"),
    }
    for field_name, value in synthesis_level_prose.items():
        for prose_error in _human_prose_errors(value):
            errors.append(f"{field_name}:{prose_error}")
    for index, value in enumerate(synthesis.get("boundaries", []) or []):
        for prose_error in _human_prose_errors(value):
            errors.append(f"boundaries[{index}]:{prose_error}")
    for section in (
        "evidence_threads",
        "source_contributions",
        "central_findings",
        "agreements",
        "positions",
        "contradictions",
        "boundary_conditions",
        "methodological_fault_lines",
    ):
        for row in synthesis.get(section, []) or []:
            if not isinstance(row, Mapping):
                continue
            for field_name in (
                "summary",
                "finding",
                "assertion",
                "agreement",
                "position",
                "contradiction",
                "boundary",
                "fault_line",
                "relationship",
                "text",
                "plain_english_meaning",
                "plain_english",
                "technical_result",
            ):
                for prose_error in _human_prose_errors(row.get(field_name)):
                    errors.append(f"{section}.{field_name}:{prose_error}")
            for reference in row.get("evidence", []) or []:
                if isinstance(reference, Mapping) and not _human_locator_text(
                    reference.get("locator") or ""
                ):
                    errors.append(
                        f"{section}:untraceable_human_locator:{reference.get('source_id', '')}"
                    )
    return errors


def validate_cluster_synthesis(
    value: Any,
    cluster: Mapping[str, Any],
    profiles: Sequence[Mapping[str, Any]],
    *,
    deterministic_debate: Mapping[str, Any] | None = None,
    all_clusters: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Admit exact propositions and explicitly typed family-level assertions."""
    raw = _as_mapping(value) if value else {}
    cluster_id = str(cluster.get("cluster_id") or "")
    if raw.get("cluster_id") and str(raw.get("cluster_id")) != cluster_id:
        raw = {}
    profile_by_source = {str(row["source_id"]): row for row in profiles}
    anchor_by_key = {
        (
            str(profile.get("source_id") or ""),
            str(anchor.get("evidence_anchor_id") or anchor.get("claim_id") or ""),
        ): anchor
        for profile in profiles
        for anchor in profile.get("claims", []) or []
        if isinstance(anchor, Mapping)
    }
    specific_anchor_ids_by_source: dict[str, set[str]] = defaultdict(set)
    for (source_id, anchor_id), anchor in anchor_by_key.items():
        if _anchor_is_synthesis_eligible(anchor) and not _anchor_is_composite_note_summary(
            anchor
        ):
            specific_anchor_ids_by_source[source_id].add(anchor_id)
    allowed_source_ids = {str(value) for value in cluster.get("source_ids", []) or []}
    cluster_role_by_source = {
        str(row.get("source_id") or ""): str(row.get("role") or "context")
        for row in cluster.get("source_roles", []) or []
        if isinstance(row, Mapping) and row.get("source_id")
    }
    core_source_ids = {
        source_id
        for source_id, role in cluster_role_by_source.items()
        if role == "core"
    } or {
        str(value) for value in cluster.get("core_source_ids", []) or [] if str(value)
    }
    raw_sections = {
        key: _sanitize_reasoned_items(
            raw.get(key, []), profile_by_source, allowed_source_ids=allowed_source_ids
        )
        for key in CLUSTER_SYNTHESIS_SECTIONS
        if key != "related_clusters"
    }
    raw_sections["related_clusters"] = []
    cluster_by_id = {
        str(row.get("cluster_id") or ""): row
        for row in all_clusters
        if isinstance(row, Mapping) and row.get("cluster_id")
    }
    validated_related_clusters: list[dict[str, Any]] = []
    for value in raw.get("related_clusters", []) or []:
        if not isinstance(value, Mapping):
            continue
        item = _as_mapping(value)
        target_id = str(
            item.get("target_cluster_id")
            or item.get("related_cluster_id")
            or item.get("cluster_id")
            or ""
        )
        target_cluster = cluster_by_id.get(target_id)
        if target_cluster is None or target_id == cluster_id:
            continue
        target_source_ids = {
            str(source_id)
            for source_id in target_cluster.get("source_ids", []) or []
            if str(source_id)
        }
        current_values = item.get("current_evidence") or item.get("evidence") or []
        target_values = item.get("target_evidence") or []
        current_evidence = _resolve_reasoner_evidence(
            current_values,
            profile_by_source,
            allowed_source_ids=allowed_source_ids,
        )
        target_evidence = _resolve_reasoner_evidence(
            target_values,
            profile_by_source,
            allowed_source_ids=target_source_ids,
        )
        relationship = _human_projection_text(
            item.get("relationship")
            or item.get("explanation")
            or item.get("summary")
            or item.get("text")
            or ""
        )
        relationship_terms = (
            _tokens(relationship)
            - _GENERIC_FAMILY_RELATION_TERMS
            - _BROAD_FIELD_TERMS
        )
        current_terms = _tokens(
            [
                anchor_by_key.get(
                    (
                        str(reference.get("source_id") or ""),
                        str(
                            reference.get("evidence_anchor_id")
                            or reference.get("claim_id")
                            or ""
                        ),
                    ),
                    {},
                ).get("text")
                for reference in current_evidence
            ]
        )
        target_terms = _tokens(
            [
                anchor_by_key.get(
                    (
                        str(reference.get("source_id") or ""),
                        str(
                            reference.get("evidence_anchor_id")
                            or reference.get("claim_id")
                            or ""
                        ),
                    ),
                    {},
                ).get("text")
                for reference in target_evidence
            ]
        )
        if (
            not relationship
            or not current_evidence
            or not target_evidence
            or not (relationship_terms & current_terms)
            or not (relationship_terms & target_terms)
        ):
            continue
        validated_related_clusters.append(
            {
                "target_cluster_id": target_id,
                "cluster_id": target_id,
                "target_label": _human_projection_text(
                    target_cluster.get("display_label")
                    or target_cluster.get("label")
                    or target_id
                ),
                "relation_type": str(item.get("relation_type") or "intellectual_bridge"),
                "relationship": relationship,
                "evidence": current_evidence,
                "current_evidence": current_evidence,
                "target_evidence": target_evidence,
            }
        )
    proposition_by_id = {
        str(row.get("proposition_id") or ""): row
        for row in cluster.get("propositions", []) or []
        if row.get("proposition_id")
    }
    proposition_ids_by_anchor: dict[tuple[str, str], set[str]] = defaultdict(set)
    for proposition_id, proposition in proposition_by_id.items():
        for reference in proposition.get("evidence", []) or []:
            proposition_ids_by_anchor[
                (
                    str(reference.get("source_id") or ""),
                    str(
                        reference.get("evidence_anchor_id")
                        or reference.get("claim_id")
                        or ""
                    ),
                )
            ].add(proposition_id)
    family_relation_types_by_anchor: dict[tuple[str, str], set[str]] = defaultdict(set)
    for relation in cluster.get("family_relations", []) or []:
        if not isinstance(relation, Mapping):
            continue
        relation_type = str(relation.get("relation_type") or "")
        if relation_type not in FAMILY_RELATION_TYPES:
            continue
        for reference in relation.get("evidence", []) or []:
            family_relation_types_by_anchor[
                (
                    str(reference.get("source_id") or ""),
                    str(
                        reference.get("evidence_anchor_id")
                        or reference.get("claim_id")
                        or ""
                    ),
                )
            ].add(relation_type)
    cluster_theme_terms = _cluster_theme_terms(cluster) | (
        _tokens(
            [
                [row.get("title"), row.get("question")]
                for row in raw_sections.get("evidence_threads", [])
            ]
        )
        - _GENERIC_FAMILY_RELATION_TERMS
        - _BROAD_FIELD_TERMS
    )
    cluster_lineage_anchor_keys = {
        *proposition_ids_by_anchor,
        *family_relation_types_by_anchor,
    }

    def reference_is_cluster_relevant(
        reference: Mapping[str, Any],
        *,
        statement_terms: set[str] | None = None,
    ) -> bool:
        source_id = str(reference.get("source_id") or "")
        anchor_id = str(
            reference.get("evidence_anchor_id") or reference.get("claim_id") or ""
        )
        if (source_id, anchor_id) in cluster_lineage_anchor_keys:
            return True
        profile = profile_by_source.get(source_id, {})
        anchor = next(
            (
                row
                for row in profile.get("claims", []) or []
                if str(row.get("evidence_anchor_id") or row.get("claim_id") or "")
                == anchor_id
            ),
            None,
        )
        return bool(
            anchor
            and _anchor_matches_cluster_theme(
                anchor,
                cluster_theme_terms | (statement_terms or set()),
            )
        )

    deterministic_state = str(
        (deterministic_debate or {}).get("evidence_classification")
        or (deterministic_debate or {}).get("classification")
        or raw.get("debate_state")
        or ""
    )
    if deterministic_state not in DEBATE_STATES:
        deterministic_state = ""
    proposition_state_by_id = {
        str(row.get("proposition_id") or ""): str(row.get("state") or "")
        for row in (deterministic_debate or {}).get("proposition_assessments", []) or []
        if isinstance(row, Mapping) and row.get("proposition_id")
    }
    sections: dict[str, list[dict[str, Any]]] = {
        key: [] for key in CLUSTER_SYNTHESIS_SECTIONS
    }
    allowed_thread_relationships = {
        "complementary",
        "complementary_positions",
        "conditional",
        "conditional_relationship",
        "interpretive",
        "methodological",
        "parallel",
        "parallel_literatures",
        "sequential",
    }
    rejected_assertions: list[dict[str, Any]] = []
    quantitative_comparisons: list[dict[str, Any]] = []
    for section, items in raw_sections.items():
        for item in items:
            evidence = list(item.get("evidence", []) or [])
            if section == "source_contributions" and len(evidence) == 1:
                reference = evidence[0]
                reference_key = (
                    str(reference.get("source_id") or ""),
                    str(
                        reference.get("evidence_anchor_id")
                        or reference.get("claim_id")
                        or ""
                    ),
                )
                canonical_anchor = anchor_by_key.get(reference_key)
                if canonical_anchor is not None and _anchor_is_composite_note_summary(
                    canonical_anchor
                ) and specific_anchor_ids_by_source.get(reference_key[0]):
                    source_profile = profile_by_source.get(reference_key[0], {})
                    contribution_statement = str(
                        item.get("finding")
                        or item.get("assertion")
                        or item.get("text")
                        or ""
                    )
                    statement_terms = _comparability_tokens(contribution_statement)
                    specific_candidates = [
                        anchor
                        for anchor in source_profile.get("claims", []) or []
                        if _anchor_is_synthesis_eligible(anchor)
                        and not _anchor_is_composite_note_summary(anchor)
                        and _anchor_matches_cluster_theme(anchor, cluster_theme_terms)
                    ]
                    if specific_candidates:
                        canonical_anchor = min(
                            specific_candidates,
                            key=lambda anchor: (
                                not bool(
                                    proposition_ids_by_anchor.get(
                                        (
                                            reference_key[0],
                                            str(
                                                anchor.get("evidence_anchor_id")
                                                or anchor.get("claim_id")
                                                or ""
                                            ),
                                        )
                                    )
                                ),
                                -len(
                                    statement_terms
                                    & _comparability_tokens(anchor.get("text"))
                                ),
                                -len(
                                    cluster_theme_terms
                                    & _tokens(
                                        [
                                            anchor.get("text"),
                                            anchor.get("topic"),
                                            anchor.get("dimensions"),
                                        ]
                                    )
                                ),
                                len(str(anchor.get("text") or "")),
                                str(
                                    anchor.get("evidence_anchor_id")
                                    or anchor.get("claim_id")
                                    or ""
                                ),
                            ),
                        )
                if canonical_anchor is not None:
                    # The reasoner chooses which anchor matters to the cluster;
                    # the anchor itself owns the wording, figures, plain-English
                    # explanation, and locator. This prevents a valid ID from
                    # being paired with a model-invented number or page.
                    item["finding"] = str(
                        canonical_anchor.get("text")
                        or canonical_anchor.get("claim")
                        or ""
                    )
                    item["plain_english_meaning"] = str(
                        canonical_anchor.get("plain_english_meaning") or ""
                    )
                    item["technical_result"] = _anchor_technical_result(
                        canonical_anchor
                    )
                    evidence = [_evidence_ref(canonical_anchor)]
                    item["evidence"] = evidence
                    item["canonical_anchor_projection"] = True
            thread_support_failed = False
            if section == "evidence_threads":
                thread_statement = str(
                    item.get("summary")
                    or item.get("assertion")
                    or item.get("finding")
                    or item.get("text")
                    or ""
                ).strip()
                thread_statement, evidence = _reconcile_evidence_thread_support(
                    item,
                    thread_statement,
                    evidence,
                    profile_by_source,
                )
                item["evidence"] = evidence
                thread_support_failed = not bool(thread_statement and evidence)
            inferred_propositions = sorted(
                {
                    proposition_id
                    for reference in evidence
                    for proposition_id in proposition_ids_by_anchor.get(
                        (
                            str(reference.get("source_id") or ""),
                            str(
                                reference.get("evidence_anchor_id")
                                or reference.get("claim_id")
                                or ""
                            ),
                        ),
                        set(),
                    )
                }
            )
            supplied = [
                str(value)
                for value in (
                    item.get("proposition_ids")
                    or (
                        [item.get("proposition_id")]
                        if item.get("proposition_id")
                        else []
                    )
                )
                if str(value) in proposition_by_id
            ]
            proposition_ids = sorted(set(supplied or inferred_propositions))
            assertion_proposition_states = {
                proposition_state_by_id.get(proposition_id, deterministic_state)
                for proposition_id in proposition_ids
            }
            family_relation_types = sorted(
                {
                    relation_type
                    for reference in evidence
                    for relation_type in family_relation_types_by_anchor.get(
                        (
                            str(reference.get("source_id") or ""),
                            str(
                                reference.get("evidence_anchor_id")
                                or reference.get("claim_id")
                                or ""
                            ),
                        ),
                        set(),
                    )
                }
            )
            statement = str(
                item.get("assertion")
                or item.get("finding")
                or item.get("position")
                or item.get("agreement")
                or item.get("contradiction")
                or item.get("text")
                or item.get("summary")
                or ""
            ).strip()
            statement_terms = (
                _tokens(statement) - _GENERIC_FAMILY_RELATION_TERMS - _BROAD_FIELD_TERMS
            )
            rejection_reason = (
                "evidence_thread_sentence_not_supported_by_locator"
                if thread_support_failed
                else ""
            )
            thread_relationship_type = ""
            if section == "evidence_threads" and len(
                {str(reference.get("source_id") or "") for reference in evidence}
            ) > 1:
                relationship = str(item.get("relationship") or "").strip()
                relationship_prefix = re.split(
                    r"\s*[:;—-]\s*", relationship, maxsplit=1
                )[0]
                thread_relationship_type = slugify(relationship_prefix).replace(
                    "-", "_"
                )
                if thread_relationship_type in allowed_thread_relationships:
                    item["relationship_type"] = thread_relationship_type
            effective_evidence_bases = {
                evidence_base_id
                for reference in evidence
                if (evidence_base_id := _reference_evidence_base_id(reference))
            }
            evidence_restrictions = " ".join(
                str(value)
                for reference in evidence
                for value in _as_mapping(
                    anchor_by_key.get(
                        (
                            str(reference.get("source_id") or ""),
                            str(
                                reference.get("evidence_anchor_id")
                                or reference.get("claim_id")
                                or ""
                            ),
                        ),
                        {},
                    ).get("support_envelope")
                ).get("restrictions", [])
                or []
                if str(value)
            )
            if not item.get("quantitative_results"):
                item["quantitative_results"] = _evidence_quantitative_results(
                    evidence, profile_by_source
                )
            quantitative_errors = _quantitative_item_errors(
                item,
                require_comparable=section
                in {"central_findings", "agreements", "positions", "contradictions"},
            )
            has_quantitative_content = bool(
                item.get("quantitative_results")
                or item.get("quantitative_comparisons")
                or item.get("statistics")
                or item.get("technical_context")
                or item.get("technical_result")
                or item.get("technical_detail")
            )
            if has_quantitative_content:
                quantitative_result_ids = sorted(
                    str(row.get("quantitative_result_id") or "")
                    for row in item.get("quantitative_results", []) or []
                    if isinstance(row, Mapping) and row.get("quantitative_result_id")
                )
                quantitative_comparisons.append(
                    {
                        "comparison_id": f"quantitative-comparison-{_stable_hash([cluster_id, section, item])[:16]}",
                        "proposition_id": proposition_ids[0]
                        if len(proposition_ids) == 1
                        else "",
                        "source_ids": sorted(
                            {
                                str(row.get("source_id") or "")
                                for row in evidence
                                if row.get("source_id")
                            }
                        ),
                        "quantitative_result_ids": quantitative_result_ids,
                        "status": (
                            "rejected"
                            if quantitative_errors
                            else "valid"
                            if len(quantitative_result_ids) >= 2
                            or item.get("quantitative_comparisons")
                            else "qualified"
                        ),
                        "estimands_comparable": not any(
                            "estimand" in error for error in quantitative_errors
                        ),
                        "outcomes_comparable": not any(
                            "outcome" in error for error in quantitative_errors
                        ),
                        "populations_comparable": not any(
                            "population" in error for error in quantitative_errors
                        ),
                        "arithmetic_reproducible": not any(
                            "arithmetic" in error or "percentage_point" in error
                            for error in quantitative_errors
                        ),
                        "reason": (
                            ";".join(quantitative_errors)
                            if quantitative_errors
                            else "deterministic_quantitative_checks_passed"
                        ),
                        "qualifications": quantitative_errors,
                    }
                )
            if (
                section != "source_contributions"
                and quantitative_errors
                and not _has_numerical_claim(statement)
            ):
                # A bad optional numerical gloss must not erase a supported
                # qualitative proposition. Preserve the rejected comparison in
                # the machine audit, drop only the unverified arithmetic, and
                # retain source-specific figures in the contribution section.
                for field_name in (
                    "technical_result",
                    "technical_detail",
                    "technical_context",
                    "statistics",
                    "quantitative_results",
                    "quantitative_comparisons",
                ):
                    item.pop(field_name, None)
                item["plain_english_meaning"] = (
                    "In plain English, the collection supports the direction of this relationship. "
                    "The source-specific figures remain separate below because their quantitative "
                    "comparability was not verified."
                )
                item["quantitative_detail_status"] = "omitted_unvalidated_comparison"
                quantitative_errors = []
            if rejection_reason:
                pass
            elif section == "source_contributions":
                contribution_source_id = str(
                    item.get("source_id")
                    or (evidence[0].get("source_id") if evidence else "")
                )
                if not contribution_source_id or any(
                    str(reference.get("source_id") or "") != contribution_source_id
                    for reference in evidence
                ):
                    rejection_reason = "source_contribution_mixes_sources"
                elif (
                    len(evidence) == 1
                    and (
                        composite_key := (
                            contribution_source_id,
                            str(
                                evidence[0].get("evidence_anchor_id")
                                or evidence[0].get("claim_id")
                                or ""
                            ),
                        )
                    )
                    and _anchor_is_composite_note_summary(
                        anchor_by_key.get(composite_key, {})
                    )
                    and specific_anchor_ids_by_source.get(contribution_source_id)
                ):
                    rejection_reason = "composite_anchor_has_specific_alternative"
                elif not evidence or not any(
                    reference_is_cluster_relevant(
                        reference, statement_terms=statement_terms
                    )
                    for reference in evidence
                ):
                    rejection_reason = "source_contribution_not_cluster_relevant"
                item["source_id"] = contribution_source_id
                item["cluster_role"] = cluster_role_by_source.get(
                    contribution_source_id, "context"
                )
                supplied_comparison = str(item.get("comparison_status") or "")
                item["comparison_status"] = (
                    "context_only"
                    if item["cluster_role"] in {"context", "bridge"}
                    else supplied_comparison
                    if supplied_comparison
                    in {
                        "single_source",
                        "supports_shared_pattern",
                        "contrasts_with_shared_pattern",
                    }
                    else "single_source"
                )
                item["related_proposition_ids"] = proposition_ids
                item["evidence_thread_id"] = str(item.get("evidence_thread_id") or "")
                supplied_kind = str(item.get("contribution_kind") or "")
                allowed_kinds = {
                    "direct_proposition_finding",
                    "unique_cluster_relevant_finding",
                    "boundary_evidence",
                    "methodological_context",
                    "conceptual_context",
                    "bridge_evidence",
                }
                item["contribution_kind"] = (
                    supplied_kind
                    if supplied_kind in allowed_kinds
                    else "bridge_evidence"
                    if item["cluster_role"] == "bridge"
                    else "conceptual_context"
                    if item["cluster_role"] == "context"
                    else "direct_proposition_finding"
                    if proposition_ids
                    else "unique_cluster_relevant_finding"
                )
                item["technical_result"] = str(
                    item.get("technical_result")
                    or item.get("technical_detail")
                    or item.get("technical_context")
                    or item.get("statistics")
                    or ""
                )
                item["relation_to_cluster_question"] = str(
                    item.get("relation_to_cluster_question")
                    or (
                        "Direct evidence for an admitted proposition."
                        if proposition_ids
                        else "A cluster-relevant source finding retained without a cross-source comparison."
                    )
                )
                plain_english = str(item.get("plain_english_meaning") or "")
                if (
                    _has_unqualified_causal_language(statement)
                    or _has_unqualified_causal_language(plain_english)
                ) and not any(
                    _anchor_supports_causal_claim(
                        anchor_by_key.get(
                            (
                                str(reference.get("source_id") or ""),
                                str(
                                    reference.get("evidence_anchor_id")
                                    or reference.get("claim_id")
                                    or ""
                                ),
                            ),
                            {},
                        )
                    )
                    for reference in evidence
                ):
                    boundary = _causal_support_boundary(
                        value
                        for reference in evidence
                        for value in _as_mapping(
                            anchor_by_key.get(
                                (
                                    str(reference.get("source_id") or ""),
                                    str(
                                        reference.get("evidence_anchor_id")
                                        or reference.get("claim_id")
                                        or ""
                                    ),
                                ),
                                {},
                            ).get("support_envelope")
                        ).get("restrictions", [])
                        or []
                    )
                    statement = _narrow_noncausal_organizational_language(
                        statement, boundary=boundary
                    )
                    _replace_cluster_item_statement(item, statement)
                    item["plain_english_meaning"] = (
                        _narrow_noncausal_organizational_language(
                            plain_english, boundary=boundary
                        )
                        if plain_english
                        else "This evidence does not by itself establish causation."
                    )
                    item["attribution_narrowed"] = True
                if quantitative_errors:
                    rejection_reason = ";".join(quantitative_errors)
            elif section == "evidence_threads" and (
                {
                    str(source_id)
                    for source_id in item.get("source_ids", []) or []
                    if str(source_id)
                }
                - {
                    str(reference.get("source_id") or "")
                    for reference in evidence
                    if reference.get("source_id")
                }
            ):
                rejection_reason = "evidence_thread_missing_source_specific_locator"
            elif re.search(
                r"\brobust\b[^.!?]{0,80}\bselection", statement, flags=re.I
            ) and re.search(
                r"\b(?:selection|two-step|heckman)\b",
                evidence_restrictions,
                flags=re.I,
            ):
                rejection_reason = (
                    "robustness_claim_conflicts_with_selection_limitation"
                )
            elif re.search(r"\bindependent\b", statement, flags=re.I) and any(
                not bool(reference.get("counted_as_independent"))
                for reference in evidence
            ):
                rejection_reason = "independence_claim_exceeds_verified_lineage"
            elif section in {
                "evidence_threads",
                "boundary_conditions",
                "methodological_fault_lines",
            } and not all(
                reference_is_cluster_relevant(
                    reference, statement_terms=statement_terms
                )
                for reference in evidence
            ):
                rejection_reason = "organizational_statement_not_cluster_relevant"
            elif (
                section == "evidence_threads"
                and len(
                    {str(reference.get("source_id") or "") for reference in evidence}
                )
                > 1
                and thread_relationship_type not in allowed_thread_relationships
            ):
                rejection_reason = (
                    "multi_source_thread_requires_non_strict_relationship"
                )
            elif (
                not proposition_ids
                and not family_relation_types
                and section
                not in {
                    "evidence_threads",
                    "boundary_conditions",
                    "methodological_fault_lines",
                }
            ):
                rejection_reason = (
                    "assertion_not_linked_to_proposition_or_family_relation"
                )
            elif section in {"agreements", "contradictions"} and not proposition_ids:
                rejection_reason = "strict_comparison_requires_proposition_lineage"
            elif (
                bool(cluster.get("parallel_candidate_merge"))
                and len(proposition_ids) > 1
            ):
                rejection_reason = "assertion_spans_parallel_propositions_without_cross_proposition_relation"
            elif _named_attribution_missing_source_ids(
                statement,
                evidence,
                profile_by_source,
                allowed_source_ids,
            ):
                rejection_reason = "named_source_attribution_not_supported_by_cited_source"
            elif section != "source_contributions" and _universal_claim_missing_core_coverage(
                statement,
                evidence,
                core_source_ids,
            ):
                rejection_reason = "universal_claim_missing_core_source_coverage"
            elif section != "source_contributions" and _universal_direction_conflicts_with_evidence(
                statement,
                evidence,
                anchor_by_key,
            ):
                rejection_reason = (
                    "universal_direction_claim_conflicts_with_cited_anchors"
                )
            elif _has_unqualified_causal_language(statement) and not any(
                _anchor_supports_causal_claim(
                    anchor_by_key.get(
                        (
                            str(reference.get("source_id") or ""),
                            str(
                                reference.get("evidence_anchor_id")
                                or reference.get("claim_id")
                                or ""
                            ),
                        ),
                        {},
                    )
                )
                for reference in evidence
            ):
                if section in {
                    "evidence_threads",
                    "boundary_conditions",
                    "methodological_fault_lines",
                }:
                    boundary = _causal_support_boundary(
                        value
                        for reference in evidence
                        for value in _as_mapping(
                            anchor_by_key.get(
                                (
                                    str(reference.get("source_id") or ""),
                                    str(
                                        reference.get("evidence_anchor_id")
                                        or reference.get("claim_id")
                                        or ""
                                    ),
                                ),
                                {},
                            ).get("support_envelope")
                        ).get("restrictions", [])
                        or []
                    )
                    statement = _narrow_noncausal_organizational_language(
                        statement, boundary=boundary
                    )
                    _replace_cluster_item_statement(item, statement)
                    item["causal_language_narrowed"] = True
                    if item.get("plain_english_meaning"):
                        item["plain_english_meaning"] = (
                            _narrow_noncausal_organizational_language(
                                item.get("plain_english_meaning"), boundary=boundary
                            )
                        )
                else:
                    rejection_reason = (
                        "causal_wording_without_causal_or_mechanism_anchor"
                    )
            elif not effective_evidence_bases and section not in {
                "evidence_threads",
                "boundary_conditions",
                "methodological_fault_lines",
            }:
                rejection_reason = "assertion_has_no_independent_source_support"
            elif (
                section in {"central_findings", "agreements", "contradictions"}
                and proposition_ids
                and len(effective_evidence_bases) < 2
            ):
                rejection_reason = (
                    "comparative_assertion_requires_two_effective_evidence_bases"
                )
            elif section in {
                "central_findings",
                "agreements",
                "positions",
                "contradictions",
            } and any(
                cluster_role_by_source.get(
                    str(reference.get("source_id") or ""), "context"
                )
                != "core"
                for reference in evidence
            ):
                rejection_reason = (
                    "context_or_bridge_source_cannot_support_comparative_verdict"
                )
            elif (
                section != "source_contributions"
                and _asserts_consensus(statement)
                and "mapped_consensus" not in assertion_proposition_states
            ):
                rejection_reason = (
                    "consensus_strength_language_without_mature_consensus"
                )
            elif section == "agreements" and not assertion_proposition_states & {
                "mapped_consensus",
                "emerging_convergence",
                "aligned_institutional_guidance",
                "within_program_consistency",
            }:
                rejection_reason = (
                    "agreement_not_supported_by_deterministic_relationship_state"
                )
            elif (
                section == "agreements"
                and "mapped_consensus" in assertion_proposition_states
                and len(effective_evidence_bases) < 3
            ):
                rejection_reason = (
                    "mapped_consensus_requires_three_effective_evidence_bases"
                )
            elif (
                section == "contradictions"
                and "mapped_debate" not in assertion_proposition_states
            ):
                rejection_reason = (
                    "contradiction_not_supported_by_deterministic_comparison"
                )
            elif section == "contradictions" and re.match(
                r"^\s*no\s+(?:direct\s+)?contradictions?\b",
                statement,
                flags=re.IGNORECASE,
            ):
                rejection_reason = "absence_of_contradiction_is_not_a_contradiction"
            elif quantitative_errors:
                rejection_reason = ";".join(quantitative_errors)
            if rejection_reason:
                rejected_assertions.append(
                    {
                        "section": section,
                        "reason": rejection_reason,
                        "statement": statement,
                        "evidence": evidence,
                    }
                )
                continue
            if section == "source_contributions":
                contribution_id = str(
                    item.get("contribution_id")
                    or f"contribution-{_stable_hash([cluster_id, item['source_id'], evidence, statement])[:16]}"
                )
                sections[section].append(
                    {
                        "contribution_id": contribution_id,
                        "source_id": item["source_id"],
                        "cluster_role": item["cluster_role"],
                        "contribution_kind": item["contribution_kind"],
                        "related_proposition_ids": item["related_proposition_ids"],
                        "evidence_thread_id": item["evidence_thread_id"],
                        "finding": statement,
                        "technical_result": item["technical_result"],
                        "plain_english_meaning": str(
                            item.get("plain_english_meaning") or ""
                        ),
                        "relation_to_cluster_question": item[
                            "relation_to_cluster_question"
                        ],
                        "comparison_status": item["comparison_status"],
                        "evidence": evidence,
                    }
                )
                continue
            semantic_item = {
                key: value
                for key, value in item.items()
                if key
                not in {
                    "item_id",
                    "assertion_id",
                    "updated_at",
                    "evidence",
                    "supporting_evidence",
                }
            }
            assertion_id = (
                f"assertion-{slugify(section)}-"
                f"{_stable_hash([cluster_id, section, proposition_ids, semantic_item])[:12]}"
            )
            item["assertion_id"] = assertion_id
            item["item_id"] = assertion_id
            item["proposition_ids"] = proposition_ids
            item["family_relation_types"] = family_relation_types
            sections[section].append(item)
    sections["related_clusters"] = validated_related_clusters
    deduplicated_contributions: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    for contribution in sections["source_contributions"]:
        anchor_ids = tuple(
            sorted(
                {
                    str(
                        reference.get("evidence_anchor_id")
                        or reference.get("claim_id")
                        or ""
                    )
                    for reference in contribution.get("evidence", []) or []
                    if str(
                        reference.get("evidence_anchor_id")
                        or reference.get("claim_id")
                        or ""
                    )
                }
            )
        )
        finding_identity = _canonical_phrase(contribution.get("finding") or "")
        key = (
            str(contribution.get("source_id") or ""),
            (f"finding:{finding_identity}",) if finding_identity else anchor_ids,
        )
        prior = deduplicated_contributions.get(key)
        if prior is None or sum(
            len(str(contribution.get(field_name) or ""))
            for field_name in ("finding", "plain_english_meaning", "technical_result")
        ) > sum(
            len(str(prior.get(field_name) or ""))
            for field_name in ("finding", "plain_english_meaning", "technical_result")
        ):
            deduplicated_contributions[key] = contribution
    sections["source_contributions"] = list(deduplicated_contributions.values())
    for contribution in sections["source_contributions"]:
        contribution["origin"] = "reasoner"
    fallback_contributions = _fallback_source_contributions(cluster, profiles)
    existing_contribution_keys = {
        (
            str(row.get("source_id") or ""),
            str(
                (_as_mapping(row.get("evidence", [{}])[0])).get("evidence_anchor_id")
                or ""
            )
            if row.get("evidence")
            else "",
        )
        for row in sections["source_contributions"]
    }
    existing_contribution_findings = {
        (
            str(row.get("source_id") or ""),
            _canonical_phrase(row.get("finding") or ""),
        )
        for row in sections["source_contributions"]
        if _canonical_phrase(row.get("finding") or "")
    }
    for contribution in fallback_contributions:
        contribution = dict(contribution)
        contribution["origin"] = "deterministic_profile_fallback"
        fallback_quantitative_errors = _quantitative_item_errors(
            contribution,
            require_comparable=False,
        )
        if fallback_quantitative_errors:
            evidence = [
                _as_mapping(reference)
                for reference in contribution.get("evidence", []) or []
                if isinstance(reference, Mapping)
            ]
            source_ids = sorted(
                {
                    str(reference.get("source_id") or "")
                    for reference in evidence
                    if reference.get("source_id")
                }
            )
            quantitative_results = _evidence_quantitative_results(
                evidence, profile_by_source
            )
            quantitative_comparisons.append(
                {
                    "comparison_id": (
                        f"quantitative-comparison-"
                        f"{_stable_hash([cluster_id, 'source_contributions', contribution])[:16]}"
                    ),
                    "proposition_id": str(
                        (contribution.get("related_proposition_ids") or [""])[0]
                    ),
                    "source_ids": source_ids,
                    "quantitative_result_ids": sorted(
                        str(row.get("quantitative_result_id") or "")
                        for row in quantitative_results
                        if row.get("quantitative_result_id")
                    ),
                    "status": "rejected",
                    "estimands_comparable": not any(
                        "estimand" in error for error in fallback_quantitative_errors
                    ),
                    "outcomes_comparable": not any(
                        "outcome" in error for error in fallback_quantitative_errors
                    ),
                    "populations_comparable": not any(
                        "population" in error for error in fallback_quantitative_errors
                    ),
                    "arithmetic_reproducible": not any(
                        "arithmetic" in error or "percentage_point" in error
                        for error in fallback_quantitative_errors
                    ),
                    "reason": ";".join(fallback_quantitative_errors),
                    "qualifications": fallback_quantitative_errors,
                }
            )
            rejected_assertions.append(
                {
                    "section": "source_contributions",
                    "reason": ";".join(fallback_quantitative_errors),
                    "statement": str(contribution.get("finding") or ""),
                    "evidence": evidence,
                }
            )
            if _has_numerical_claim(contribution.get("finding")):
                contribution["finding"] = (
                    "The source reports a cluster-relevant quantitative finding, but its exact figures "
                    "are omitted here because the stored numerical summaries did not pass arithmetic validation."
                )
            contribution["technical_result"] = ""
            contribution["plain_english_meaning"] = (
                "The source supports the stated relationship, but its stored numerical summaries could not be "
                "reconciled as one comparison. Consult the linked source locator for the separate estimates."
            )
        reference = _as_mapping((contribution.get("evidence") or [{}])[0])
        key = (
            str(contribution.get("source_id") or ""),
            str(reference.get("evidence_anchor_id") or ""),
        )
        finding_key = (
            str(contribution.get("source_id") or ""),
            _canonical_phrase(contribution.get("finding") or ""),
        )
        if (
            key in existing_contribution_keys
            or finding_key in existing_contribution_findings
        ):
            continue
        sections["source_contributions"].append(contribution)
        existing_contribution_keys.add(key)
        if finding_key[1]:
            existing_contribution_findings.add(finding_key)
    capped_contributions: list[dict[str, Any]] = []
    contribution_counts: Counter[str] = Counter()
    for contribution in sections["source_contributions"]:
        source_id = str(contribution.get("source_id") or "")
        limit = 3 if cluster_role_by_source.get(source_id) == "core" else 1
        if contribution_counts[source_id] >= limit:
            continue
        contribution_counts[source_id] += 1
        capped_contributions.append(contribution)
    sections["source_contributions"] = capped_contributions

    verdict_sections = (
        "evidence_threads",
        "central_findings",
        "agreements",
        "positions",
        "contradictions",
    )
    verdict_core_source_ids = {
        str(reference.get("source_id") or "")
        for section in verdict_sections
        for row in sections[section]
        for reference in row.get("evidence", []) or []
        if cluster_role_by_source.get(str(reference.get("source_id") or ""), "context")
        == "core"
    }
    model_multi_source_verdict_present = any(
        len(
            {
                str(reference.get("source_id") or "")
                for reference in row.get("evidence", []) or []
                if cluster_role_by_source.get(
                    str(reference.get("source_id") or ""), "context"
                )
                == "core"
            }
        )
        >= 2
        for section in verdict_sections
        for row in sections[section]
    )
    core_source_count = sum(
        1 for role in cluster_role_by_source.values() if role == "core"
    )
    minimum_verdict_source_count = min(2, core_source_count)
    core_contribution_source_ids = {
        str(row.get("source_id") or "")
        for row in sections["source_contributions"]
        if cluster_role_by_source.get(str(row.get("source_id") or ""), "context")
        == "core"
    }
    model_comparative_assertion_present = any(
        not str(row.get("origin") or "").startswith("deterministic_")
        for section in verdict_sections
        for row in sections[section]
    )
    model_validated_content_present = model_comparative_assertion_present or any(
        str(row.get("origin") or "") == "reasoner"
        for row in sections["source_contributions"]
    )
    if len(core_contribution_source_ids) >= 2 and (
        len(verdict_core_source_ids) < minimum_verdict_source_count
        or not model_multi_source_verdict_present
    ):
        contribution_thread = _source_contribution_map_thread(
            cluster,
            sections["source_contributions"],
            profile_by_source,
            cluster_role_by_source,
        )
        if contribution_thread is not None:
            thread_id = str(contribution_thread["thread_id"])
            sections["evidence_threads"].append(contribution_thread)
            for contribution in sections["source_contributions"]:
                source_id = str(contribution.get("source_id") or "")
                if cluster_role_by_source.get(
                    source_id, "context"
                ) == "core" and not contribution.get("evidence_thread_id"):
                    contribution["evidence_thread_id"] = thread_id

    # If a substantive reasoner synthesis covers one admitted proposition but
    # omits another, add a deliberately non-substantive coverage assertion.
    # It copies only the already admitted proposition and evidence lineage; it
    # cannot create agreement, contradiction, causal support, or quantitative
    # interpretation. An otherwise empty/invalid model response is never
    # upgraded by this fallback alone.
    model_assertions = [
        item
        for section, items in sections.items()
        if section != "source_contributions"
        for item in items
        if not str(item.get("origin") or "").startswith("deterministic_")
    ]
    covered_proposition_ids = {
        str(proposition_id)
        for item in model_assertions
        for proposition_id in item.get("proposition_ids", []) or []
        if str(proposition_id)
    }
    if model_assertions:
        for proposition_id, proposition in sorted(proposition_by_id.items()):
            if proposition_id in covered_proposition_ids:
                continue
            if int(proposition.get("effective_evidence_base_count", 0) or 0) < 2:
                continue
            proposition_statement = str(proposition.get("statement") or "").strip()
            if not proposition_statement or _has_numerical_claim(proposition_statement):
                continue
            proposition_evidence = [
                dict(reference)
                for reference in proposition.get("evidence", []) or []
                if isinstance(reference, Mapping)
            ]
            if not proposition_evidence or any(
                not (source_id := str(reference.get("source_id") or ""))
                or source_id not in profile_by_source
                or cluster_role_by_source.get(source_id, "context") != "core"
                or not _reference_is_synthesis_eligible(
                    reference, profile_by_source[source_id]
                )
                for reference in proposition_evidence
            ):
                continue
            evidence_lineage = sorted(
                (
                    str(reference.get("source_id") or ""),
                    str(
                        reference.get("evidence_anchor_id")
                        or reference.get("claim_id")
                        or ""
                    ),
                    str(reference.get("locator") or ""),
                )
                for reference in proposition_evidence
            )
            assertion_id = (
                "assertion-proposition-coverage-"
                + _stable_hash([cluster_id, proposition_id, evidence_lineage])[:12]
            )
            sections["central_findings"].append(
                {
                    "assertion_id": assertion_id,
                    "item_id": assertion_id,
                    "finding": (
                        f"The located evidence also addresses the admitted proposition: “{proposition_statement}” "
                        "This records proposition coverage only; it does not establish a causal effect, consensus, "
                        "or contradiction."
                    ),
                    "plain_english_meaning": (
                        "This second line of comparison belongs in the cluster, but its source-specific findings "
                        "must be assessed in the proposition matrix rather than collapsed into the main verdict."
                    ),
                    "proposition_ids": [proposition_id],
                    "family_relation_types": [],
                    "origin": "deterministic_proposition_coverage",
                    "qualification": "proposition_coverage_only",
                    "evidence": proposition_evidence,
                }
            )
    top_evidence = _resolve_reasoner_evidence(
        raw.get("supporting_evidence", []),
        profile_by_source,
        allowed_source_ids=allowed_source_ids,
    )
    # Top-level evidence is a summary convenience, not a stricter provenance
    # boundary than the assertions themselves. Count every admitted section
    # reference when deciding whether the synthesis is genuinely multi-source.
    top_evidence = sorted(
        {
            _stable_hash(reference): reference
            for reference in [
                *top_evidence,
                *(
                    reference
                    for rows in sections.values()
                    for row in rows
                    for reference in row.get("evidence", []) or []
                ),
            ]
        }.values(),
        key=lambda row: (row["source_id"], row["claim_id"]),
    )
    comparative_evidence = [
        reference
        for section, rows in sections.items()
        if section != "source_contributions"
        for row in rows
        for reference in row.get("evidence", []) or []
    ]
    supporting_evidence_bases = {
        evidence_base_id
        for row in comparative_evidence
        if (evidence_base_id := _reference_evidence_base_id(row))
    }
    deterministic_source_map_present = any(
        row.get("origin") == "deterministic_source_contribution_map"
        for row in sections["evidence_threads"]
    )
    thematic_source_map_present = bool(
        model_validated_content_present
        and len(core_contribution_source_ids) >= 2
        and len(verdict_core_source_ids) >= minimum_verdict_source_count
        and model_multi_source_verdict_present
    )
    substantive = (
        (
            len(supporting_evidence_bases) >= 1
            and any(
                rows
                for section, rows in sections.items()
                if section != "source_contributions"
            )
        )
        or deterministic_source_map_present
        or thematic_source_map_present
    )
    gap_hypotheses = _sanitize_reasoned_items(
        raw.get("gap_hypotheses", []),
        profile_by_source,
        allowed_source_ids=allowed_source_ids,
    )
    for hypothesis in gap_hypotheses:
        evidence = list(hypothesis.get("evidence", []) or [])
        proposition_ids = sorted(
            {
                proposition_id
                for reference in evidence
                for proposition_id in proposition_ids_by_anchor.get(
                    (
                        str(reference.get("source_id") or ""),
                        str(
                            reference.get("evidence_anchor_id")
                            or reference.get("claim_id")
                            or ""
                        ),
                    ),
                    set(),
                )
            }
        )
        supplied = str(hypothesis.get("proposition_id") or "")
        if supplied in proposition_by_id:
            proposition_ids = sorted({*proposition_ids, supplied})
        if not proposition_ids:
            hypothesis["_rejected"] = True
            hypothesis["_rejection_reason"] = (
                "gap_hypothesis_missing_proposition_lineage"
            )
            continue
        hypothesis["proposition_id"] = proposition_ids[0]
        # A cluster-local synthesis may hypothesize only from its own admitted
        # proposition and anchors. Cross-cluster lineage is added later only
        # when equivalent, independently generated hypotheses merge or when
        # the collection pass supplies evidence from both clusters. Accepting
        # an arbitrary provider list here produced plausible-looking but false
        # backlinks from gaps to unrelated clusters.
        hypothesis["related_cluster_ids"] = [cluster_id]
        hypothesis["supporting_evidence"] = list(hypothesis.pop("evidence", []))
    gap_hypotheses = [row for row in gap_hypotheses if not row.pop("_rejected", False)]
    if not gap_hypotheses:
        for section in (
            "evidence_threads",
            "central_findings",
            "agreements",
            "positions",
            "contradictions",
            "boundary_conditions",
            "methodological_fault_lines",
        ):
            for item in sections[section]:
                statement = _cluster_item_text(item)
                narrowed = _narrow_unadjudicated_gap_language(statement)
                if narrowed != statement:
                    _replace_cluster_item_statement(item, narrowed)
                    item["gap_language_narrowed"] = True
                if item.get("plain_english_meaning"):
                    item["plain_english_meaning"] = _narrow_unadjudicated_gap_language(
                        item.get("plain_english_meaning")
                    )
    synthesis_assertions = sorted(
        [
            dict(item)
            for section, items in sections.items()
            if section != "source_contributions"
            for item in items
        ],
        key=lambda row: str(row.get("assertion_id") or ""),
    )
    debate_state = deterministic_state or str(raw.get("debate_state") or "")
    if debate_state not in DEBATE_STATES:
        debate_state = (
            "parallel_literatures" if deterministic_source_map_present else ""
        )
    verdict_paragraphs: list[dict[str, Any]] = []
    seen_verdict_statements: set[str] = set()
    if substantive:
        for section in (
            "evidence_threads",
            "central_findings",
            "agreements",
            "positions",
            "contradictions",
        ):
            for item in sections[section]:
                statement = _cluster_item_text(item)
                if not statement or statement.casefold() in seen_verdict_statements:
                    continue
                seen_verdict_statements.add(statement.casefold())
                plain = str(
                    item.get("plain_english_meaning") or item.get("plain_english") or ""
                ).strip()
                technical = str(
                    item.get("technical_result")
                    or item.get("technical_detail")
                    or item.get("technical_context")
                    or item.get("statistics")
                    or ""
                ).strip()
                paragraph = " ".join(
                    value for value in (statement, plain, technical) if value
                )
                verdict_paragraphs.append(
                    {
                        "verdict_id": f"verdict-{_stable_hash([cluster_id, item.get('assertion_id'), paragraph])[:16]}",
                        "section": section,
                        "text": paragraph,
                        "assertion_ids": [str(item.get("assertion_id") or "")],
                        "proposition_ids": [
                            str(value)
                            for value in item.get("proposition_ids", []) or []
                        ],
                        "evidence": list(item.get("evidence", []) or []),
                    }
                )
    synthesis_text = "\n\n".join(row["text"] for row in verdict_paragraphs)
    result = {
        "cluster_id": cluster_id,
        "status": "deterministic_fallback",
        "scope": str(raw.get("scope") or cluster.get("shared_question") or ""),
        "boundaries": [
            str(value) for value in raw.get("boundaries", []) or [] if str(value)
        ],
        "coherence_rationale": str(
            raw.get("coherence_rationale")
            if substantive
            else cluster.get("coherence_rationale") or ""
        ),
        "synthesis": synthesis_text,
        "verdict_paragraphs": verdict_paragraphs,
        "synthesis_assertions": synthesis_assertions,
        "debate_state": debate_state,
        "rejected_assertions": rejected_assertions,
        "supporting_evidence": top_evidence,
        "effective_evidence_base_count": len(supporting_evidence_bases),
        "gap_hypotheses": gap_hypotheses,
        "quantitative_comparisons": quantitative_comparisons,
        "strict_adjudications": [
            dict(row)
            for row in (deterministic_debate or {}).get("strict_adjudications", [])
            or []
            if isinstance(row, Mapping)
        ],
        **sections,
    }
    if substantive:
        quality_errors = _cluster_synthesis_quality_errors(result, cluster)
        result["quality_errors"] = quality_errors
        result["quality_status"] = "complete" if not quality_errors else "incomplete"
        deterministic_source_map_only = bool(
            not model_validated_content_present and deterministic_source_map_present
        )
        result["status"] = (
            "deterministic_fallback"
            if not quality_errors and deterministic_source_map_only
            else "reasoned"
            if not quality_errors
            else "partial"
        )
    else:
        result["quality_errors"] = []
        result["quality_status"] = "not_applicable"
    return result


def apply_cluster_syntheses_to_debates(
    debate_registry: Mapping[str, Any],
    syntheses: Mapping[str, Mapping[str, Any]],
    *,
    policy: Any = None,
) -> dict[str, Any]:
    """Use validated reasoned proposition groups without weakening promotion gates."""
    auto_promote = bool(_policy_value(policy, "auto_promote_debates", True))
    assessments: list[dict[str, Any]] = []
    for original in debate_registry.get("assessments", []) or []:
        assessment = dict(original)
        synthesis = syntheses.get(str(assessment.get("cluster_id") or ""), {})
        contradictions = list(synthesis.get("contradictions", []) or [])
        positions = list(synthesis.get("positions", []) or [])
        agreements = list(synthesis.get("agreements", []) or [])
        # Deterministic comparable-proposition gates remain authoritative.
        # Reasoner prose can explain an admitted classification but cannot
        # create or erase a debate by itself.
        evidence_classification = str(
            assessment.get("evidence_classification")
            or assessment.get("classification")
            or "no_debate"
        )
        if evidence_classification == "debate":
            evidence_classification = "mapped_debate"
        if evidence_classification not in DEBATE_STATES:
            evidence_classification = "no_debate"
        detected_debate = evidence_classification == "mapped_debate"
        promoted = detected_debate and auto_promote
        classification = evidence_classification
        assessment.update(
            {
                "classification": classification,
                "evidence_classification": evidence_classification,
                "status": classification,
                "promoted": promoted,
                "automation_status": "promoted" if promoted else "mapped",
                "positions": positions
                if classification
                in {"mapped_debate", "complementary_positions", "parallel_literatures"}
                and positions
                else assessment.get("positions", []),
                "agreements": agreements
                if agreements
                else assessment.get("agreements", []),
                "contradictions": contradictions
                if contradictions
                else assessment.get("contradictions", []),
                "contradiction_groups": (
                    [
                        {
                            "proposition": str(
                                row.get("proposition")
                                or row.get("contradiction")
                                or row.get("text")
                                or ""
                            ),
                            "positions": [],
                            "supporting_evidence": list(row.get("evidence", []) or []),
                        }
                        for row in contradictions
                    ]
                    if detected_debate and contradictions
                    else assessment.get("contradiction_groups", [])
                ),
                "boundaries": list(
                    synthesis.get("boundary_conditions", [])
                    or assessment.get("boundaries", [])
                ),
                "method_fault_lines": list(
                    synthesis.get("methodological_fault_lines", [])
                    or assessment.get("method_fault_lines", [])
                ),
                "synthesis_status": str(
                    synthesis.get("status") or "deterministic_fallback"
                ),
            }
        )
        assessments.append(assessment)
    debates = [row for row in assessments if row.get("promoted")]
    candidates: list[dict[str, Any]] = []
    return {
        "debates": sorted(debates, key=lambda row: row["cluster_id"]),
        "debate_candidates": sorted(candidates, key=lambda row: row["cluster_id"]),
        "assessments": sorted(assessments, key=lambda row: row["cluster_id"]),
        "debate_count": len(debates),
        "debate_candidate_count": len(candidates),
    }


_DIRECTIONAL_PROPOSITION_TOKENS = {
    "association",
    "conditional",
    "decrease",
    "decreased",
    "heterogeneous",
    "higher",
    "improve",
    "improved",
    "increase",
    "increased",
    "lower",
    "null",
    "reduce",
    "reduced",
    "significant",
    "support",
    "undermine",
    "vary",
}


def _claim_proposition_parts(
    claim: Mapping[str, Any],
) -> tuple[set[str], set[str], set[str]]:
    dimensions = (
        claim.get("dimensions", {})
        if isinstance(claim.get("dimensions"), Mapping)
        else {}
    )
    topic_terms = _tokens(claim.get("topic", ""))
    outcome_terms = _tokens(dimensions.get("outcome", []))
    relationship_terms = _tokens(
        [
            claim.get("text", ""),
            dimensions.get("mechanism", []),
            dimensions.get("theory", []),
        ]
    )
    relationship_terms -= topic_terms | outcome_terms | _DIRECTIONAL_PROPOSITION_TOKENS
    return topic_terms, outcome_terms, relationship_terms


def _same_semantic_proposition(
    left: Mapping[str, Any], right: Mapping[str, Any]
) -> bool:
    left_topic, left_outcome, left_relationship = _claim_proposition_parts(left)
    right_topic, right_outcome, right_relationship = _claim_proposition_parts(right)
    if left_topic and right_topic and not (left_topic & right_topic):
        return False
    if left_outcome and right_outcome and not (left_outcome & right_outcome):
        return False
    if left_relationship or right_relationship:
        if not left_relationship or not right_relationship:
            return False
        shared_relationship = left_relationship & right_relationship
        relationship_jaccard = len(shared_relationship) / max(
            1, len(left_relationship | right_relationship)
        )
        if len(shared_relationship) < 2 or relationship_jaccard < 0.65:
            return False
    return bool(
        (left_topic & right_topic)
        or (left_outcome & right_outcome)
        or (left_relationship & right_relationship)
    )


def _shared_proposition_identity(
    left: Mapping[str, Any], right: Mapping[str, Any]
) -> str:
    left_topic, left_outcome, left_relationship = _claim_proposition_parts(left)
    right_topic, right_outcome, right_relationship = _claim_proposition_parts(right)
    shared_topic = left_topic & right_topic
    if shared_topic:
        return " ".join(sorted(shared_topic))
    shared = (left_outcome & right_outcome) | (left_relationship & right_relationship)
    if not shared:
        shared = _tokens(left.get("text", "")) & _tokens(right.get("text", ""))
        shared -= _DIRECTIONAL_PROPOSITION_TOKENS
    return " ".join(sorted(shared))


def _build_dimensional_evidence_matrices_legacy(
    profiles: Sequence[Any],
    clusters: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Build matrices whose cells contain only claim/source/locator-backed records."""
    rows = _ensure_profiles(profiles)
    by_source = {row["source_id"]: row for row in rows}
    matrices: list[dict[str, Any]] = []
    for cluster in clusters:
        entries_by_dimension: dict[str, list[dict[str, Any]]] = {}
        omitted_unlocated = 0
        for dimension in EVIDENCE_DIMENSIONS:
            by_value: dict[str, dict[str, Any]] = {}
            for source_id in cluster.get("source_ids", []) or []:
                for claim in by_source.get(str(source_id), {}).get("claims", []) or []:
                    values = claim.get("dimensions", {}).get(dimension, []) or []
                    if values and not claim.get("locator_complete"):
                        omitted_unlocated += 1
                        continue
                    for value in values:
                        identity = _canonical_phrase(value) or str(value).casefold()
                        cell = by_value.setdefault(
                            identity, {"value": str(value), "evidence": []}
                        )
                        reference = _evidence_ref(claim)
                        if reference not in cell["evidence"]:
                            cell["evidence"].append(reference)
            entries = []
            for identity, cell in sorted(by_value.items()):
                evidence = sorted(
                    cell["evidence"],
                    key=lambda row: (row["source_id"], row["claim_id"], row["locator"]),
                )
                entries.append(
                    {
                        "value_id": f"matrix-value-{_stable_hash([cluster['cluster_id'], dimension, identity])[:12]}",
                        "value": cell["value"],
                        "source_count": len({row["source_id"] for row in evidence}),
                        "study_family_count": len(
                            {row["study_family_id"] for row in evidence}
                        ),
                        "evidence": evidence,
                    }
                )
            entries_by_dimension[dimension] = entries
        matrices.append(
            {
                "matrix_id": f"matrix-{_stable_hash(cluster['cluster_id'])[:12]}",
                "cluster_id": cluster["cluster_id"],
                "dimensions": entries_by_dimension,
                "dimension_names": list(EVIDENCE_DIMENSIONS),
                "locator_backed_only": True,
                "omitted_unlocated_cell_count": omitted_unlocated,
            }
        )
    return sorted(matrices, key=lambda row: row["cluster_id"])


def build_evidence_matrices(
    profiles: Sequence[Any],
    clusters: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Build proposition rows by core source; never cross-product metadata."""

    rows = _ensure_profiles(profiles)
    profile_by_source = {str(row["source_id"]): row for row in rows}
    matrices: list[dict[str, Any]] = []
    for cluster in clusters:
        core_source_ids = [
            str(value) for value in cluster.get("core_source_ids", []) or []
        ]
        proposition_rows: list[dict[str, Any]] = []
        for proposition in cluster.get("propositions", []) or []:
            cells: dict[str, dict[str, Any]] = {}
            for raw_cell in proposition.get("cells", []) or []:
                source_id = str(raw_cell.get("source_id") or "")
                if source_id not in core_source_ids:
                    continue
                valid_evidence = [
                    dict(reference)
                    for reference in raw_cell.get("evidence", []) or []
                    if isinstance(reference, Mapping)
                    and (
                        (profile := profile_by_source.get(source_id)) is not None
                        and _reference_is_synthesis_eligible(reference, profile)
                    )
                ]
                if not valid_evidence:
                    continue
                cells[source_id] = {
                    "source_id": source_id,
                    "study_family_id": str(
                        raw_cell.get("study_family_id") or source_id
                    ),
                    "evidence_base_group_id": str(
                        raw_cell.get("evidence_base_group_id")
                        or profile_by_source[source_id].get("evidence_base_group_id")
                        or ""
                    ),
                    "counted_as_independent": bool(
                        raw_cell.get("counted_as_independent")
                        if "counted_as_independent" in raw_cell
                        else profile_by_source[source_id].get("evidence_base_counted")
                    ),
                    "stance_or_finding": str(raw_cell.get("stance_or_finding") or ""),
                    "evidence_type": list(raw_cell.get("evidence_type", []) or []),
                    "scope": dict(raw_cell.get("scope") or {}),
                    "boundary_conditions": list(
                        raw_cell.get("boundary_conditions", []) or []
                    ),
                    "direction_or_interpretation": list(
                        raw_cell.get("direction_or_interpretation", []) or []
                    ),
                    "uncertainty": list(raw_cell.get("uncertainty", []) or []),
                    "evidence": valid_evidence,
                }
            family_count = len(
                {
                    str(cell.get("study_family_id") or source_id)
                    for source_id, cell in cells.items()
                }
            )
            evidence_base_count = len(
                {
                    str(cell.get("evidence_base_group_id") or "")
                    for source_id, cell in cells.items()
                    if cell.get("counted_as_independent") is True
                    and cell.get("evidence_base_group_id")
                }
            )
            if len(cells) < 2:
                continue
            proposition_rows.append(
                {
                    "proposition_id": str(proposition.get("proposition_id") or ""),
                    "statement": str(proposition.get("statement") or ""),
                    "question": str(proposition.get("question") or ""),
                    "proposition_type": str(
                        proposition.get("proposition_type") or "unknown"
                    ),
                    "comparability": dict(proposition.get("comparability") or {}),
                    "independent_core_study_family_count": family_count,
                    "effective_evidence_base_count": evidence_base_count,
                    "publication_count": len(cells),
                    "admission_eligible": evidence_base_count >= 2,
                    "cells": {
                        source_id: cells[source_id] for source_id in sorted(cells)
                    },
                }
            )
        family_relations = []
        for relation in cluster.get("family_relations", []) or []:
            if not isinstance(relation, Mapping):
                continue
            relation_source_ids = [
                str(source_id)
                for source_id in relation.get("source_ids", []) or []
                if str(source_id) in core_source_ids
            ]
            relation_evidence = _resolve_reasoner_evidence(
                relation.get("evidence", []) or [],
                profile_by_source,
                allowed_source_ids=set(relation_source_ids),
            )
            family_relations.append(
                {
                    "relation_type": str(relation.get("relation_type") or ""),
                    "source_ids": relation_source_ids,
                    "rationale": str(relation.get("rationale") or ""),
                    "comparability": dict(_as_mapping(relation.get("comparability"))),
                    "evidence": relation_evidence,
                }
            )
        family_relations = [
            relation
            for relation in family_relations
            if relation["relation_type"] in FAMILY_RELATION_TYPES
            and len(set(relation["source_ids"])) >= 2
            and {
                str(reference.get("source_id") or "")
                for reference in relation["evidence"]
            }
            >= set(relation["source_ids"])
        ]
        matrices.append(
            {
                "matrix_id": f"matrix-{_stable_hash([cluster['cluster_id'], PROPOSITION_MATRIX_VERSION])[:12]}",
                "matrix_version": PROPOSITION_MATRIX_VERSION,
                "cluster_id": str(cluster["cluster_id"]),
                "core_source_ids": core_source_ids,
                "propositions": proposition_rows,
                "proposition_count": len(proposition_rows),
                "family_relations": family_relations,
                "family_relation_count": len(family_relations),
                "locator_backed_only": True,
                "source_level_metadata_inherited": False,
                "exact_proposition_gate_passed": any(
                    row.get("admission_eligible") for row in proposition_rows
                ),
                "admission_passed": bool(cluster.get("family_admission_passed"))
                and _family_relation_graph_connected(
                    set(core_source_ids), family_relations
                ),
            }
        )
    return sorted(matrices, key=lambda row: row["cluster_id"])


def _build_debate_registry_legacy(
    profiles: Sequence[Any],
    clusters: Sequence[Mapping[str, Any]],
    *,
    policy: Any = None,
) -> dict[str, Any]:
    """Classify debates only when two independently located positions exist."""
    rows = _ensure_profiles(profiles)
    by_source = {row["source_id"]: row for row in rows}
    assessments: list[dict[str, Any]] = []
    debates: list[dict[str, Any]] = []
    debate_candidates: list[dict[str, Any]] = []
    auto_promote = bool(_policy_value(policy, "auto_promote_debates", True))
    for cluster in clusters:
        claims = [
            claim
            for source_id in cluster.get("source_ids", []) or []
            for claim in by_source.get(str(source_id), {}).get("claims", []) or []
            if claim.get("locator_complete")
        ]
        substantive = [
            claim
            for claim in claims
            if str(claim.get("direction") or "not_reported")
            not in {"not_reported", "mixed"}
        ]
        comparable_pairs = [
            (left, right)
            for left, right in combinations(substantive, 2)
            if _same_semantic_proposition(left, right)
            and str(left.get("study_family_id")) != str(right.get("study_family_id"))
        ]
        opposing_pairs = [
            (left, right)
            for left, right in comparable_pairs
            if str(left.get("direction")) != str(right.get("direction"))
        ]
        agreement_pairs = [
            (left, right)
            for left, right in comparable_pairs
            if str(left.get("direction")) == str(right.get("direction"))
        ]
        if opposing_pairs:
            classification = "debate"
        elif agreement_pairs:
            classification = "mapped_consensus"
        elif len(claims) >= 2:
            classification = "mixed_evidence"
        else:
            classification = "no_debate"

        debate_claims = {
            (str(claim.get("source_id")), str(claim.get("claim_id"))): claim
            for pair in opposing_pairs
            for claim in pair
        }
        agreement_claims = {
            (str(claim.get("source_id")), str(claim.get("claim_id"))): claim
            for pair in agreement_pairs
            for claim in pair
        }
        by_position: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for claim in debate_claims.values():
            by_position[str(claim.get("direction"))].append(claim)
        positions = []
        for direction, evidence in sorted(by_position.items()):
            refs = sorted(
                (_evidence_ref(claim) for claim in evidence),
                key=lambda row: (row["source_id"], row["claim_id"]),
            )
            positions.append({"position": direction, "evidence": refs})
        agreements = []
        by_agreement: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for claim in agreement_claims.values():
            by_agreement[str(claim.get("direction"))].append(claim)
        for direction, evidence in sorted(by_agreement.items()):
            agreements.append(
                {
                    "finding_direction": direction,
                    "evidence": sorted(
                        (_evidence_ref(claim) for claim in evidence),
                        key=lambda row: (row["source_id"], row["claim_id"]),
                    ),
                }
            )
        contradictions = []
        grouped_contradictions: dict[str, dict[tuple[str, str], Mapping[str, Any]]] = (
            defaultdict(dict)
        )
        for left, right in sorted(
            opposing_pairs,
            key=lambda pair: (
                str(pair[0].get("study_family_id")),
                str(pair[0].get("source_id")),
                str(pair[1].get("study_family_id")),
                str(pair[1].get("source_id")),
            ),
        ):
            proposition = _shared_proposition_identity(left, right) or str(
                cluster.get("semantic_identity") or ""
            )
            contradictions.append(
                {
                    "proposition": proposition,
                    "position_a": str(left.get("direction")),
                    "position_b": str(right.get("direction")),
                    "evidence": [_evidence_ref(left), _evidence_ref(right)],
                }
            )
            for claim in (left, right):
                grouped_contradictions[proposition][
                    (str(claim.get("source_id")), str(claim.get("claim_id")))
                ] = claim
        contradiction_groups = []
        for proposition, grouped_claims in sorted(grouped_contradictions.items()):
            grouped_positions: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for claim in grouped_claims.values():
                grouped_positions[str(claim.get("direction"))].append(
                    _evidence_ref(claim)
                )
            contradiction_groups.append(
                {
                    "proposition": proposition,
                    "positions": [
                        {
                            "position": direction,
                            "evidence": sorted(
                                evidence,
                                key=lambda row: (row["source_id"], row["claim_id"]),
                            ),
                        }
                        for direction, evidence in sorted(grouped_positions.items())
                    ],
                    "supporting_evidence": sorted(
                        (_evidence_ref(claim) for claim in grouped_claims.values()),
                        key=lambda row: (row["source_id"], row["claim_id"]),
                    ),
                }
            )
        boundaries = []
        for claim in claims:
            boundary_values = [
                claim.get("boundary_condition"),
                *(claim.get("dimensions", {}).get("case", []) or []),
                *(claim.get("dimensions", {}).get("period", []) or []),
            ]
            for value in _flatten_values(boundary_values):
                boundaries.append(
                    {"boundary": value, "evidence": [_evidence_ref(claim)]}
                )
        method_fault_lines = []
        if classification == "debate":
            seen_faults: set[tuple[str, str]] = set()
            for claim in debate_claims.values():
                for method in claim.get("dimensions", {}).get("method", []) or []:
                    key = (_canonical_phrase(method), str(claim.get("direction")))
                    if key in seen_faults:
                        continue
                    seen_faults.add(key)
                    method_fault_lines.append(
                        {
                            "method": method,
                            "finding_direction": claim.get("direction"),
                            "evidence": [_evidence_ref(claim)],
                        }
                    )
        detected_debate = classification == "debate"
        promoted = detected_debate and auto_promote
        visible_classification = (
            "debate_candidate" if detected_debate and not promoted else classification
        )
        assessment = {
            "debate_id": f"debate-{_stable_hash(cluster['cluster_id'])[:12]}",
            "cluster_id": cluster["cluster_id"],
            "classification": visible_classification,
            "evidence_classification": classification,
            "status": "mapped_debate"
            if promoted
            else ("debate_candidate" if detected_debate else classification),
            "promoted": promoted,
            "automation_status": "promoted"
            if promoted
            else ("candidate" if detected_debate else "not_applicable"),
            "positions": positions if detected_debate else [],
            "agreements": agreements,
            "contradictions": contradictions,
            "contradiction_groups": contradiction_groups,
            "boundaries": sorted(
                boundaries,
                key=lambda row: (row["boundary"], row["evidence"][0]["source_id"]),
            ),
            "method_fault_lines": sorted(
                method_fault_lines,
                key=lambda row: (str(row["method"]), str(row["finding_direction"])),
            ),
            "evidence_claim_count": len(claims),
        }
        assessments.append(assessment)
        if promoted:
            debates.append(assessment)
        elif detected_debate:
            debate_candidates.append(assessment)
    return {
        "debates": sorted(debates, key=lambda row: row["cluster_id"]),
        "debate_candidates": sorted(
            debate_candidates, key=lambda row: row["cluster_id"]
        ),
        "assessments": sorted(assessments, key=lambda row: row["cluster_id"]),
        "debate_count": len(debates),
        "debate_candidate_count": len(debate_candidates),
    }


def _cell_direction(cell: Mapping[str, Any]) -> str:
    values = [
        str(value)
        for value in cell.get("direction_or_interpretation", []) or []
        if str(value)
    ]
    normalized = {
        direction
        for value in values
        if (direction := _normalize_direction(value))
        in {"positive", "negative", "null", "mixed"}
    }
    normalized.discard("not_reported")
    return (
        next(iter(normalized))
        if len(normalized) == 1
        else ("mixed" if normalized else "not_reported")
    )


def _proposition_debate_state(
    proposition: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    cells = [
        dict(cell)
        for cell in _as_mapping(proposition.get("cells")).values()
        if isinstance(cell, Mapping)
    ]
    if not cells:
        return "no_debate", {}
    if len(cells) == 1:
        return "single_position", {}
    evidence_roles = {
        str(role)
        for cell in cells
        for role in cell.get("evidence_type", []) or []
        if str(role)
    }
    empirical = evidence_roles & EMPIRICAL_SUPPORT_ROLES
    argumentative = evidence_roles & ARGUMENT_SUPPORT_ROLES
    evidence_base_count = int(
        proposition.get("effective_evidence_base_count")
        or len(
            {
                str(cell.get("evidence_base_group_id") or "")
                for cell in cells
                if cell.get("counted_as_independent") is True
                and cell.get("evidence_base_group_id")
            }
        )
    )
    publication_count = int(proposition.get("publication_count") or len(cells))
    if publication_count >= 2 and evidence_base_count == 1:
        return "within_program_consistency", {"publication_count": publication_count}
    if evidence_base_count < 2:
        return "mixed_evidence", {
            "publication_count": publication_count,
            "effective_evidence_base_count": evidence_base_count,
            "reason": "independence_unresolved",
        }
    if empirical and argumentative:
        return "complementary_positions", {}
    directions = [_cell_direction(cell) for cell in cells]
    reported = {value for value in directions if value not in {"not_reported", "mixed"}}
    boundaries = {
        _canonical_phrase(cell.get("boundary_conditions", []))
        for cell in cells
        if _canonical_phrase(cell.get("boundary_conditions", []))
    }
    if len(reported) >= 2:
        comparability = _as_mapping(proposition.get("comparability"))
        if bool(comparability.get("direction_orientation_aligned", True)):
            return "mapped_debate", {"directions": sorted(reported)}
        stance_text = " ".join(
            str(cell.get("stance_or_finding") or "") for cell in cells
        )
        explicit_positive = bool(
            re.search(
                r"\b(?:positive(?:ly)?|increas(?:e|es|ed)|higher|more likely)\b",
                stance_text,
                re.I,
            )
        )
        explicit_negative = bool(
            re.search(
                r"\b(?:negative(?:ly)?|decreas(?:e|es|ed)|lower|less likely|reduc(?:e|es|ed))\b",
                stance_text,
                re.I,
            )
        )
        if explicit_positive and explicit_negative:
            return "mapped_debate", {"directions": sorted(reported)}
        return "mixed_evidence", {
            "directions": sorted(reported),
            "reason": "direction_orientation_unresolved",
        }
    if "mixed" in directions:
        return "mixed_evidence", {"directions": sorted(set(directions))}
    if len(boundaries) >= 2:
        return "conditional_relationship", {"boundary_sets": sorted(boundaries)}
    if len(reported) == 1:
        if argumentative == {"practitioner_guidance"}:
            return "aligned_institutional_guidance", {"direction": next(iter(reported))}
        return (
            "mapped_consensus" if evidence_base_count >= 3 else "emerging_convergence",
            {
                "direction": next(iter(reported)),
                "effective_evidence_base_count": evidence_base_count,
            },
        )

    stance_tokens = [_tokens(cell.get("stance_or_finding", "")) for cell in cells]
    common = (
        set.intersection(*stance_tokens)
        if stance_tokens and all(stance_tokens)
        else set()
    )
    if common and len(common) >= 2:
        if argumentative == {"practitioner_guidance"}:
            return "aligned_institutional_guidance", {"shared_terms": sorted(common)}
        return (
            "mapped_consensus" if evidence_base_count >= 3 else "emerging_convergence",
            {
                "shared_terms": sorted(common),
                "effective_evidence_base_count": evidence_base_count,
            },
        )
    if argumentative:
        opposition = any(
            re.search(
                r"\b(?:reject|oppose|cannot|should not|incompatible|contrary|fails?)\b",
                str(cell.get("stance_or_finding") or ""),
                re.I,
            )
            for cell in cells
        )
        return ("mapped_debate" if opposition else "complementary_positions"), {}
    return "mixed_evidence", {}


def _family_only_debate_state(
    family_relations: Sequence[Mapping[str, Any]],
) -> tuple[str, dict[str, Any]]:
    """Describe a connected family without pretending it is an exact comparison."""

    relation_types = {
        str(relation.get("relation_type") or "")
        for relation in family_relations
        if str(relation.get("relation_type") or "") in FAMILY_RELATION_TYPES
    }
    if not relation_types:
        return "no_debate", {"reason": "no_located_family_relation"}
    if relation_types & {"complementary_mechanism", "sequential_relationship"}:
        return "complementary_positions", {"relation_types": sorted(relation_types)}
    if "boundary_contrast" in relation_types:
        return "conditional_relationship", {"relation_types": sorted(relation_types)}
    if "methodological_fault_line" in relation_types:
        return "mixed_evidence", {"relation_types": sorted(relation_types)}
    if relation_types & {
        "rival_explanation",
        "interpretive_or_normative_disagreement",
    }:
        return "parallel_literatures", {"relation_types": sorted(relation_types)}
    return "parallel_literatures", {"relation_types": sorted(relation_types)}


def _strict_claim_adjudications(
    *,
    cluster: Mapping[str, Any],
    state: str,
    proposition_assessments: Sequence[Mapping[str, Any]],
    supporting_evidence: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Explain strict consensus and contradiction gates, including failures."""

    def assessment_evidence(
        assessment: Mapping[str, Any] | None,
    ) -> list[dict[str, Any]]:
        if not assessment:
            return [
                dict(row) for row in supporting_evidence if isinstance(row, Mapping)
            ]
        return [
            dict(reference)
            for cell in _as_mapping(assessment.get("cells")).values()
            for reference in cell.get("evidence", []) or []
            if isinstance(reference, Mapping)
        ]

    def assessment_has_opposing_positions(
        assessment: Mapping[str, Any] | None,
    ) -> bool:
        if not assessment:
            return False
        cells = [
            dict(cell)
            for cell in _as_mapping(assessment.get("cells")).values()
            if isinstance(cell, Mapping)
        ]
        if len(cells) < 2:
            return False
        reported = {
            direction
            for cell in cells
            if (direction := _cell_direction(cell)) not in {"not_reported", "mixed"}
        }
        comparability = _as_mapping(assessment.get("comparability"))
        if len(reported) >= 2 and bool(
            comparability.get("direction_orientation_aligned", True)
        ):
            return True
        stance_text = " ".join(
            str(cell.get("stance_or_finding") or "") for cell in cells
        )
        explicit_positive = bool(
            re.search(
                r"\b(?:positive(?:ly)?|increas(?:e|es|ed)|higher|more likely)\b",
                stance_text,
                re.I,
            )
        )
        explicit_negative = bool(
            re.search(
                r"\b(?:negative(?:ly)?|decreas(?:e|es|ed)|lower|less likely|reduc(?:e|es|ed))\b",
                stance_text,
                re.I,
            )
        )
        explicit_argument_opposition = bool(
            re.search(
                r"\b(?:reject|oppose|cannot|should not|incompatible|contrary|fails?)\b",
                stance_text,
                re.I,
            )
        )
        return (explicit_positive and explicit_negative) or explicit_argument_opposition

    consensus_assessment = next(
        (
            row
            for row in proposition_assessments
            if row.get("state") == "mapped_consensus"
        ),
        proposition_assessments[0] if proposition_assessments else None,
    )
    contradiction_assessment = next(
        (row for row in proposition_assessments if row.get("state") == "mapped_debate"),
        proposition_assessments[0] if proposition_assessments else None,
    )
    consensus_state = str((consensus_assessment or {}).get("state") or state)
    contradiction_state = str((contradiction_assessment or {}).get("state") or state)
    consensus_evidence = assessment_evidence(consensus_assessment)
    contradiction_evidence = assessment_evidence(contradiction_assessment)
    consensus_evidence_base_count = len(
        {
            evidence_base_id
            for row in consensus_evidence
            if (evidence_base_id := _reference_evidence_base_id(row))
        }
    )
    contradiction_evidence_base_count = len(
        {
            evidence_base_id
            for row in contradiction_evidence
            if (evidence_base_id := _reference_evidence_base_id(row))
        }
    )
    cluster_id = str(cluster.get("cluster_id") or "")
    consensus_established = consensus_state == "mapped_consensus"
    contradiction_opposition_passed = assessment_has_opposing_positions(
        contradiction_assessment
    )
    consensus_alignment_passed = consensus_state in {
        "mapped_consensus",
        "emerging_convergence",
        "aligned_institutional_guidance",
        "within_program_consistency",
    }
    consensus_scope_passed = consensus_assessment is not None
    contradiction_scope_passed = contradiction_assessment is not None
    contradiction_established = (
        contradiction_scope_passed
        and contradiction_evidence_base_count >= 2
        and contradiction_opposition_passed
    )
    relationship_description = {
        "emerging_convergence": (
            "The sources point in the same direction, but fewer than three independent evidence bases address the same proposition."
        ),
        "within_program_consistency": (
            "The aligned publications reuse one underlying evidence base, so publication count does not establish independent consensus."
        ),
        "complementary_positions": (
            "The sources illuminate different parts of the question rather than independently reaching the same comparable conclusion."
        ),
        "conditional_relationship": (
            "The apparent relationship changes across explicit boundaries, so a single unconditional consensus would erase those conditions."
        ),
        "mixed_evidence": (
            "The evidence cannot yet be reduced to one common estimand, mechanism, interpretation, or direction."
        ),
        "parallel_literatures": (
            "The sources are connected within the debate family but do not test the same proposition closely enough for a consensus claim."
        ),
        "single_position": (
            "Only one located position addresses the proposition, which is insufficient for a multi-source consensus."
        ),
        "no_debate": (
            "No comparable multi-source proposition was available for a strict consensus assessment."
        ),
    }
    consensus_explanation = (
        "At least three independent evidence bases reach a comparable conclusion on the same proposition."
        if consensus_established
        else relationship_description.get(
            consensus_state,
            "The evidence does not meet every strict consensus requirement.",
        )
    )
    contradiction_explanation = (
        "Comparable evidence bases reach opposing positions on the same proposition."
        if contradiction_established
        else (
            "Opposing positions are visible, but they do not come from at least two independent evidence bases."
            if contradiction_opposition_passed and contradiction_evidence_base_count < 2
            else (
                "Differences in the family are complementary, conditional, methodological, or otherwise non-comparable; they do not establish a direct contradiction."
                if contradiction_state
                in {
                    "complementary_positions",
                    "conditional_relationship",
                    "mixed_evidence",
                    "parallel_literatures",
                }
                else "No pair of comparable proposition cells supports opposing positions."
            )
        )
    )
    return [
        {
            "kind": "consensus",
            "candidate": str(
                (consensus_assessment or {}).get("statement")
                or cluster.get("shared_question")
                or cluster.get("label")
                or "Consensus in this family"
            ),
            "decision": "established" if consensus_established else "not_established",
            "checks": [
                {
                    "requirement": "The sources address at least one comparable proposition.",
                    "passed": consensus_assessment is not None,
                    "explanation": (
                        "A comparable proposition row was admitted for this candidate."
                        if consensus_assessment is not None
                        else "The family connection is broader than an exact proposition match."
                    ),
                },
                {
                    "requirement": "At least three independent evidence bases support the same conclusion.",
                    "passed": consensus_evidence_base_count >= 3,
                    "explanation": "The supporting proposition evidence contains "
                    + _counted_noun(
                        consensus_evidence_base_count, "independent evidence base"
                    )
                    + ".",
                },
                {
                    "requirement": "Outcomes, populations, concepts, and estimands or interpretations are comparable.",
                    "passed": consensus_scope_passed,
                    "explanation": (
                        "The proposition row passed the scope and concept comparability gate."
                        if consensus_scope_passed
                        else "No exact comparable proposition row was admitted."
                    ),
                },
                {
                    "requirement": "The comparable conclusions align rather than conflict or vary by condition.",
                    "passed": consensus_alignment_passed,
                    "explanation": (
                        "The admitted proposition conclusions align."
                        if consensus_alignment_passed
                        else "The relationship classification does not justify treating all positions as one aligned conclusion."
                    ),
                },
            ],
            "explanation": consensus_explanation,
            "what_would_change": (
                "No additional evidence is required for the collection-level consensus classification."
                if consensus_established
                else "Comparable evidence from enough independent studies, using aligned concepts and outcomes, could establish consensus."
            ),
            "proposition_ids": [
                str((consensus_assessment or {}).get("proposition_id") or "")
            ]
            if (consensus_assessment or {}).get("proposition_id")
            else [],
            "related_cluster_ids": [cluster_id],
            "evidence": consensus_evidence,
        },
        {
            "kind": "contradiction",
            "candidate": str(
                (contradiction_assessment or {}).get("statement")
                or cluster.get("shared_question")
                or cluster.get("label")
                or "Contradiction in this family"
            ),
            "decision": "established"
            if contradiction_established
            else "not_established",
            "checks": [
                {
                    "requirement": "The sources address the same comparable proposition.",
                    "passed": contradiction_scope_passed,
                    "explanation": (
                        "An exact comparable proposition row was admitted."
                        if contradiction_scope_passed
                        else "No exact comparable proposition row was admitted."
                    ),
                },
                {
                    "requirement": "At least two independent evidence bases address that proposition.",
                    "passed": contradiction_evidence_base_count >= 2,
                    "explanation": (
                        "The proposition evidence contains "
                        + _counted_noun(
                            contradiction_evidence_base_count,
                            "independent evidence base",
                        )
                        + ", meeting the source-count requirement."
                        if contradiction_evidence_base_count >= 2
                        else (
                            "Only "
                            + _counted_noun(
                                contradiction_evidence_base_count,
                                "independent evidence base",
                            )
                            + " could be verified; at least two are required."
                        )
                    ),
                },
                {
                    "requirement": "The comparable sources support genuinely opposing positions.",
                    "passed": contradiction_opposition_passed,
                    "explanation": (
                        "The located positions point in opposing directions."
                        if contradiction_opposition_passed
                        else contradiction_explanation
                    ),
                },
            ],
            "explanation": contradiction_explanation,
            "what_would_change": (
                "No additional evidence is required for the collection-level contradiction classification."
                if contradiction_established
                else (
                    "An independent evidence base reproducing either located position on the same proposition could establish the contradiction."
                    if contradiction_opposition_passed
                    and contradiction_evidence_base_count < 2
                    else "A located study reaching an opposing conclusion on the same proposition, population, outcome, and interpretive frame could establish a contradiction."
                )
            ),
            "proposition_ids": [
                str((contradiction_assessment or {}).get("proposition_id") or "")
            ]
            if (contradiction_assessment or {}).get("proposition_id")
            else [],
            "related_cluster_ids": [cluster_id],
            "evidence": contradiction_evidence,
        },
    ]


def build_debate_registry(
    profiles: Sequence[Any],
    clusters: Sequence[Mapping[str, Any]],
    *,
    policy: Any = None,
) -> dict[str, Any]:
    """Classify only relationships established by proposition matrix cells."""

    matrices = {
        row["cluster_id"]: row for row in build_evidence_matrices(profiles, clusters)
    }
    auto_promote = bool(_policy_value(policy, "auto_promote_debates", True))
    assessments: list[dict[str, Any]] = []
    precedence = {
        "mapped_debate": 11,
        "conditional_relationship": 10,
        "mixed_evidence": 9,
        "complementary_positions": 8,
        "mapped_consensus": 7,
        "emerging_convergence": 6,
        "aligned_institutional_guidance": 5,
        "within_program_consistency": 4,
        "parallel_literatures": 3,
        "single_position": 2,
        "no_debate": 1,
    }
    for cluster in clusters:
        matrix = matrices.get(str(cluster["cluster_id"]), {})
        proposition_assessments: list[dict[str, Any]] = []
        for proposition in matrix.get("propositions", []) or []:
            state, explanation = _proposition_debate_state(proposition)
            proposition_assessments.append(
                {
                    "proposition_id": str(proposition.get("proposition_id") or ""),
                    "statement": str(proposition.get("statement") or ""),
                    "state": state,
                    "explanation": explanation,
                    "comparability": dict(
                        _as_mapping(proposition.get("comparability"))
                    ),
                    "cells": proposition.get("cells", {}),
                }
            )
        states = [row["state"] for row in proposition_assessments]
        family_relations = list(matrix.get("family_relations", []) or [])
        family_state, family_explanation = _family_only_debate_state(family_relations)
        candidate_states = [*states, family_state]
        state = max(candidate_states, key=lambda value: precedence[value])
        proposition_source_sets = [
            {str(source_id) for source_id in _as_mapping(proposition.get("cells"))}
            for proposition in matrix.get("propositions", []) or []
        ]
        propositions_are_parallel = bool(proposition_source_sets) and all(
            not left_sources & right_sources
            for left_sources, right_sources in combinations(proposition_source_sets, 2)
        )
        propositions_are_parallel = propositions_are_parallel or bool(
            cluster.get("parallel_candidate_merge") and len(proposition_assessments) > 1
        )
        # The cluster-level classification describes the relationship among
        # its propositions. A merge creates no cross-proposition edge, so an
        # internal conditional result, mixed row, or debate cannot be promoted
        # into a relationship between those separate propositions. Their own
        # states remain visible in proposition_assessments.
        if propositions_are_parallel and len(proposition_assessments) > 1:
            state = "parallel_literatures"
        promoted = auto_promote and state == "mapped_debate"
        supporting = [
            reference
            for proposition in matrix.get("propositions", []) or []
            for cell in _as_mapping(proposition.get("cells")).values()
            for reference in cell.get("evidence", []) or []
        ]
        supporting.extend(
            reference
            for relation in family_relations
            for reference in relation.get("evidence", []) or []
        )
        supporting = list(
            {
                _stable_hash(reference): dict(reference)
                for reference in supporting
                if isinstance(reference, Mapping)
            }.values()
        )
        strict_adjudications = _strict_claim_adjudications(
            cluster=cluster,
            state=state,
            proposition_assessments=proposition_assessments,
            supporting_evidence=supporting,
        )
        assessment = {
            "debate_id": f"debate-{_stable_hash([cluster['cluster_id'], state])[:12]}",
            "cluster_id": str(cluster["cluster_id"]),
            "classification": state,
            "evidence_classification": state,
            "status": state,
            "promoted": promoted,
            "automation_status": "promoted" if promoted else "mapped",
            "proposition_assessments": proposition_assessments,
            "family_relations": family_relations,
            "family_relationship_explanation": family_explanation,
            "strict_adjudications": strict_adjudications,
            "supporting_evidence": supporting,
            "effective_evidence_base_count": len(
                {
                    evidence_base_id
                    for row in supporting
                    if (evidence_base_id := _reference_evidence_base_id(row))
                }
            ),
            "positions": [],
            "agreements": [],
            "contradictions": [],
            "contradiction_groups": [],
            "boundaries": [],
            "method_fault_lines": [],
        }
        assessments.append(assessment)
    debates = [row for row in assessments if row["status"] == "mapped_debate"]
    return {
        "debates": sorted(debates, key=lambda row: row["cluster_id"]),
        "debate_candidates": [],
        "assessments": sorted(assessments, key=lambda row: row["cluster_id"]),
        "debate_count": len(debates),
        "debate_candidate_count": 0,
    }


def _reasoner_proposals(
    reasoner: Any,
    profiles: Sequence[Mapping[str, Any]],
    clusters: Sequence[Mapping[str, Any]],
    request: Any = None,
) -> list[Any]:
    if reasoner is None:
        return []
    if isinstance(reasoner, Mapping):
        return list(reasoner.get("gap_candidates") or reasoner.get("gaps") or [])
    method = getattr(reasoner, "propose_gap_candidates", None) or getattr(
        reasoner, "propose_gaps", None
    )
    if callable(method):
        proposed = method(profiles=profiles, clusters=clusters)
        return list(proposed or [])
    detect = getattr(reasoner, "detect_gaps", None)
    if callable(detect) and request is not None:
        proposed = detect(profiles, request, context={"clusters": clusters})
        if isinstance(proposed, Mapping):
            return list(proposed.get("gap_candidates") or proposed.get("gaps") or [])
        return list(proposed or [])
    if callable(reasoner):
        proposed = reasoner(profiles=profiles, clusters=clusters)
        return list(proposed or [])
    return []


def _signal_evidence(
    signal: Mapping[str, Any],
    profile: Mapping[str, Any] | None,
    claim_lookup: Mapping[tuple[str, str], Mapping[str, Any]],
) -> list[dict[str, Any]]:
    def resolve_claim(claim_id: str, source_id: str = "") -> Mapping[str, Any] | None:
        if source_id and (source_id, claim_id) in claim_lookup:
            return claim_lookup[(source_id, claim_id)]
        profile_source_id = str((profile or {}).get("source_id") or "")
        if profile_source_id and (profile_source_id, claim_id) in claim_lookup:
            return claim_lookup[(profile_source_id, claim_id)]
        matches = [
            claim
            for (candidate_source, candidate_claim), claim in claim_lookup.items()
            if candidate_claim == claim_id
        ]
        return matches[0] if len(matches) == 1 else None

    evidence: list[dict[str, Any]] = []
    for claim_id in _flatten_values(
        signal.get("supporting_claim_ids")
        or signal.get("claim_ids")
        or signal.get("claim_id")
    ):
        claim = resolve_claim(claim_id)
        if claim is not None:
            evidence.append(_evidence_ref(claim))
    for raw in (
        signal.get("supporting_evidence", []) or signal.get("evidence", []) or []
    ):
        item = _as_mapping(raw)
        claim = resolve_claim(
            str(item.get("evidence_anchor_id") or item.get("claim_id") or ""),
            str(item.get("source_id", "")),
        )
        if claim:
            evidence.append(_evidence_ref(claim))
    if not evidence and profile is not None:
        signal_topic = _tokens(
            signal.get("topic") or signal.get("semantic_identity") or ""
        )
        signal_subject = _tokens(
            [
                signal.get("topic", ""),
                signal.get("semantic_identity", ""),
                signal.get("precise_missing_evidence", ""),
                signal.get("missing_evidence", ""),
                signal.get("gap_text", ""),
            ]
        )
        for claim in profile.get("claims", []) or []:
            claim_topic = _tokens(claim.get("topic", ""))
            claim_subject = _tokens(
                [
                    claim.get("topic", ""),
                    claim.get("text", ""),
                    claim.get("dimensions", {}),
                ]
            )
            shared_subject = signal_subject & claim_subject
            topic_match = bool(signal_topic & claim_topic)
            semantic_match = len(shared_subject) >= 2 or (
                len(shared_subject) == 1
                and min(len(signal_subject), len(claim_subject)) == 1
            )
            if topic_match or semantic_match:
                evidence.append(_evidence_ref(claim))
    unique = {(_stable_hash(row)): row for row in evidence}
    return sorted(
        unique.values(),
        key=lambda row: (row["source_id"], row["claim_id"], row["locator"]),
    )


_GENERIC_GAP_SUPPORT_TOKENS = {
    "causal",
    "conflict",
    "effectiveness",
    "evidence",
    "mediation",
    "mediator",
    "outcome",
    "peace",
    "success",
    "systematic",
}


def _gap_support_is_relevant(
    candidate: Mapping[str, Any], claim: Mapping[str, Any]
) -> bool:
    primary_terms = _tokens(
        candidate.get("topic") or candidate.get("gap_statement") or ""
    )
    subject_terms = _tokens(
        [
            candidate.get("topic", ""),
            candidate.get("gap_statement", ""),
            candidate.get("precise_missing_evidence", ""),
            candidate.get("observed_pattern", ""),
        ]
    )
    claim_terms = _tokens(
        [
            claim.get("topic", ""),
            claim.get("text", ""),
            claim.get("dimensions", {}),
        ]
    )
    primary_overlap = primary_terms & claim_terms
    subject_overlap = subject_terms & claim_terms
    return bool(
        len(primary_overlap) >= 2
        or (primary_overlap - _GENERIC_GAP_SUPPORT_TOKENS)
        or len(subject_overlap) >= 3
    )


def generate_gap_candidates(
    profiles: Sequence[Any],
    clusters: Sequence[Mapping[str, Any]],
    debate_registry: Mapping[str, Any],
    evidence_matrices: Sequence[Mapping[str, Any]] | None = None,
    *,
    reasoner: Any = None,
    request: Any = None,
    cluster_syntheses: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Generate only candidates allowed by GAP_RULES; no per-cluster fallback exists."""
    rows = _ensure_profiles(profiles)
    claim_lookup = {
        (str(claim["source_id"]), str(claim["claim_id"])): claim
        for row in rows
        for claim in row["claims"]
    }
    clusters_by_source: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for cluster in clusters:
        for source_id in cluster.get("source_ids", []) or []:
            clusters_by_source[str(source_id)].append(cluster)
    proposed: list[tuple[dict[str, Any], Mapping[str, Any] | None, str]] = []
    for profile in rows:
        if not profile["analytical"]:
            continue
        for signal in profile.get("gap_signals", []) or []:
            item = _as_mapping(signal)
            item.setdefault(
                "related_cluster_ids",
                _related_clusters_for_gap(item, profile, clusters_by_source),
            )
            if not item.get("related_cluster_ids"):
                item.setdefault(
                    "collection_level_rationale",
                    "A specific structured profile signal comes from an analytical source outside an admitted cluster.",
                )
            proposed.append((item, profile, "structured_profile_signal"))
        for signal in profile.get("author_stated_gaps", []) or []:
            item = (
                _as_mapping(signal)
                if not isinstance(signal, str)
                else {"missing_evidence": signal}
            )
            item["rule"] = "author_stated_gap"
            item.setdefault(
                "topic", next(iter(profile.get("semantic_topic_scores", {})), "")
            )
            item.setdefault(
                "related_cluster_ids",
                _related_clusters_for_gap(item, profile, clusters_by_source),
            )
            if not item.get("related_cluster_ids"):
                item.setdefault(
                    "collection_level_rationale",
                    "An author-stated research need comes from an analytical source outside an admitted cluster.",
                )
            origin = str(
                item.pop("_author_gap_origin", "author_stated_gap")
                or "author_stated_gap"
            )
            proposed.append((item, profile, origin))
    for cluster_id, synthesis in sorted((cluster_syntheses or {}).items()):
        if synthesis.get("status") != "reasoned":
            continue
        for signal in synthesis.get("gap_hypotheses", []) or []:
            item = _as_mapping(signal)
            item.setdefault("related_cluster_ids", [cluster_id])
            proposed.append((item, None, "cluster_synthesis"))
    for signal in _reasoner_proposals(reasoner, rows, clusters, request):
        proposed.append((_as_mapping(signal), None, "reasoner_proposal"))

    cluster_by_id = {str(row["cluster_id"]): row for row in clusters}
    for debate in debate_registry.get("debates", []) or []:
        cluster = cluster_by_id.get(str(debate.get("cluster_id")), {})
        groups = [
            {
                "proposition_id": assessment.get("proposition_id"),
                "proposition": assessment.get("statement"),
                "supporting_evidence": [
                    reference
                    for cell in _as_mapping(assessment.get("cells")).values()
                    for reference in cell.get("evidence", []) or []
                ],
            }
            for assessment in debate.get("proposition_assessments", []) or []
            if assessment.get("state") == "mapped_debate"
        ]
        for group in groups:
            proposition = str(
                group.get("proposition")
                or cluster.get("semantic_identity")
                or debate.get("cluster_id", "")
            )
            proposed.append(
                (
                    {
                        "rule": "contradictory_findings",
                        "topic": proposition,
                        "missing_evidence": (
                            f"Comparable evidence that adjudicates the opposing findings about {proposition} "
                            "under matched cases, measures, and periods."
                        ),
                        "related_cluster_ids": [debate.get("cluster_id")],
                        "proposition_id": str(group.get("proposition_id") or ""),
                        "originating_cluster_revision": str(
                            cluster.get("revision_hash") or ""
                        ),
                        "supporting_evidence": list(
                            group.get("supporting_evidence", []) or []
                        ),
                        "why_matters": (
                            f"The collection reports opposing directions for the same proposition about {proposition}."
                        ),
                        "contribution": (
                            f"A matched design could determine when the mapped relationship involving {proposition} changes direction."
                        ),
                    },
                    None,
                    "debate_rule",
                )
            )

    profile_by_source = {str(row["source_id"]): row for row in rows}
    for matrix in evidence_matrices or ():
        cluster = cluster_by_id.get(str(matrix.get("cluster_id")), {})
        for proposition in matrix.get("propositions", []) or []:
            methods_by_source = {
                source_id: tuple(
                    profile_by_source.get(source_id, {})
                    .get("dimensions", {})
                    .get("method", [])
                    or []
                )
                for source_id in _as_mapping(proposition.get("cells"))
            }
            nonempty = [methods for methods in methods_by_source.values() if methods]
            shared_methods = (
                set.intersection(*(set(methods) for methods in nonempty))
                if nonempty
                else set()
            )
            evidence = [
                reference
                for cell in _as_mapping(proposition.get("cells")).values()
                for reference in cell.get("evidence", []) or []
            ]
            families = {str(row.get("study_family_id")) for row in evidence}
            if (
                len(shared_methods) != 1
                or len(nonempty) != len(methods_by_source)
                or len(families) < 2
            ):
                continue
            method = sorted(shared_methods)[0]
            proposed.append(
                (
                    {
                        "rule": "methodological_concentration",
                        "topic": proposition.get("statement")
                        or cluster.get("semantic_identity", ""),
                        "missing_evidence": f"A comparable test of this proposition using a method other than {method}.",
                        "related_cluster_ids": [matrix.get("cluster_id")],
                        "proposition_id": proposition.get("proposition_id"),
                        "originating_cluster_revision": cluster.get(
                            "revision_hash", ""
                        ),
                        "supporting_evidence": evidence,
                        "why_matters": "The collection cannot distinguish a robust relationship from a method-dependent result.",
                        "contribution": "A methodologically distinct comparison would test whether the proposition survives a changed evidence strategy.",
                    },
                    None,
                    "methodological_concentration_rule",
                )
            )

    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for signal, profile, origin in proposed:
        rule = str(signal.get("rule") or signal.get("gap_type") or "")
        if rule not in GAP_RULES:
            continue
        topic = str(
            signal.get("topic") or signal.get("semantic_identity") or ""
        ).strip()
        missing = str(
            signal.get("precise_missing_evidence")
            or signal.get("missing_evidence")
            or signal.get("gap_text")
            or ""
        ).strip()
        if not topic or not missing:
            continue
        key = (rule, _canonical_phrase(topic), _canonical_phrase(missing))
        candidate = grouped.setdefault(
            key,
            {
                "gap_id": f"gap-{slugify(rule)}-{_stable_hash(key)[:12]}",
                "rule": rule,
                "topic": topic,
                "precise_missing_evidence": missing,
                "related_cluster_ids": [],
                "supporting_evidence": [],
                "countervailing_evidence": [],
                "observed_pattern": str(
                    signal.get("observed_pattern")
                    or signal.get("observed_evidence")
                    or ""
                ),
                "generation_explanation": str(
                    signal.get("generation_explanation") or ""
                ),
                "evidence_needed": str(
                    signal.get("evidence_needed")
                    or signal.get("study_needed")
                    or missing
                ),
                "why_matters": str(signal.get("why_matters") or ""),
                "contribution": str(signal.get("contribution") or ""),
                "proposal_origins": [],
                "collection_level_rationale": str(
                    signal.get("collection_level_rationale") or ""
                ),
                "proposition_ids": [],
                "originating_cluster_revisions": [],
                "missing_cell": dict(signal.get("missing_cell") or {}),
            },
        )
        candidate["related_cluster_ids"].extend(
            _flatten_values(
                signal.get("related_cluster_ids") or signal.get("related_clusters")
            )
        )
        candidate["supporting_evidence"].extend(
            _signal_evidence(signal, profile, claim_lookup)
        )
        candidate["proposal_origins"].append(origin)
        if signal.get("proposition_id"):
            candidate["proposition_ids"].append(str(signal["proposition_id"]))
        if signal.get("originating_cluster_revision"):
            candidate["originating_cluster_revisions"].append(
                str(signal["originating_cluster_revision"])
            )
        if not candidate["collection_level_rationale"] and signal.get(
            "collection_level_rationale"
        ):
            candidate["collection_level_rationale"] = str(
                signal["collection_level_rationale"]
            )
    result = []
    for candidate in grouped.values():
        candidate["related_cluster_ids"] = sorted(
            {
                str(value)
                for value in candidate["related_cluster_ids"]
                if str(value) in cluster_by_id
            }
        )
        candidate["supporting_evidence"] = sorted(
            {
                _stable_hash(row): row for row in candidate["supporting_evidence"]
            }.values(),
            key=lambda row: (row["source_id"], row["claim_id"], row["locator"]),
        )
        candidate["supporting_evidence"] = [
            reference
            for reference in candidate["supporting_evidence"]
            if (
                (
                    claim := claim_lookup.get(
                        (
                            str(reference.get("source_id") or ""),
                            str(reference.get("claim_id") or ""),
                        )
                    )
                )
                is not None
                and _gap_support_is_relevant(candidate, claim)
            )
        ]
        evidence_keys = {
            (
                str(row.get("source_id") or ""),
                str(row.get("evidence_anchor_id") or row.get("claim_id") or ""),
            )
            for row in candidate["supporting_evidence"]
        }
        for cluster_id in candidate["related_cluster_ids"]:
            cluster = cluster_by_id.get(cluster_id, {})
            for proposition in cluster.get("propositions", []) or []:
                proposition_keys = {
                    (
                        str(row.get("source_id") or ""),
                        str(row.get("evidence_anchor_id") or row.get("claim_id") or ""),
                    )
                    for row in proposition.get("evidence", []) or []
                }
                explicit_proposition_match = str(
                    proposition.get("proposition_id") or ""
                ) in set(candidate["proposition_ids"])
                if explicit_proposition_match or evidence_keys & proposition_keys:
                    candidate["proposition_ids"].append(
                        str(proposition.get("proposition_id") or "")
                    )
                    candidate["originating_cluster_revisions"].append(
                        str(cluster.get("revision_hash") or "")
                    )
        candidate["proposition_ids"] = sorted(
            {value for value in candidate["proposition_ids"] if value}
        )
        candidate["proposition_id"] = (
            candidate["proposition_ids"][0]
            if len(candidate["proposition_ids"]) == 1
            else ""
        )
        lineage_keys = {
            (
                str(reference.get("source_id") or ""),
                str(
                    reference.get("evidence_anchor_id")
                    or reference.get("claim_id")
                    or ""
                ),
            )
            for cluster_id in candidate["related_cluster_ids"]
            for proposition in cluster_by_id.get(cluster_id, {}).get("propositions", [])
            or []
            if str(proposition.get("proposition_id") or "")
            in set(candidate["proposition_ids"])
            for reference in proposition.get("evidence", []) or []
            if isinstance(reference, Mapping)
        }
        candidate["proposition_evidence_keys"] = [
            {"source_id": source_id, "evidence_anchor_id": anchor_id}
            for source_id, anchor_id in sorted(lineage_keys)
        ]
        candidate["originating_cluster_revisions"] = sorted(
            {value for value in candidate["originating_cluster_revisions"] if value}
        )
        candidate["originating_cluster_revision"] = (
            candidate["originating_cluster_revisions"][0]
            if len(candidate["originating_cluster_revisions"]) == 1
            else ""
        )
        if not candidate["missing_cell"]:
            missing_key = {
                "untested_mechanism": "mechanism",
                "empirical_coverage": "population_or_case",
                "methodological_concentration": "method",
                "measurement_or_data": "measure_or_data",
                "boundary_condition": "boundary_condition",
                "replication": "replication",
                "contradictory_findings": "adjudicating_comparison",
                "cross_cluster_integration": "cross_cluster_relationship",
                "author_stated_gap": "author_specified_relationship",
            }.get(candidate["rule"], "missing_relationship")
            candidate["missing_cell"] = {
                "kind": missing_key,
                "description": candidate["precise_missing_evidence"],
            }
        candidate["proposal_origins"] = sorted(set(candidate["proposal_origins"]))
        candidate["generation_explanation"] = candidate["generation_explanation"] or (
            f"Generated by the {candidate['rule'].replace('_', ' ')} rule from "
            f"{', '.join(candidate['proposal_origins'])}."
        )
        if not candidate["observed_pattern"] and candidate["supporting_evidence"]:
            supporting_claims = [
                claim_lookup[(str(reference["source_id"]), str(reference["claim_id"]))]
                for reference in candidate["supporting_evidence"]
                if (str(reference["source_id"]), str(reference["claim_id"]))
                in claim_lookup
            ]
            supporting_claims.sort(
                key=lambda claim: (str(claim["source_id"]), str(claim["claim_id"]))
            )
            claim_texts = [str(claim.get("text") or "") for claim in supporting_claims]
            candidate["observed_pattern"] = " ".join(
                text for text in claim_texts if text
            )[:1_200]
        candidate["why_matters"] = candidate["why_matters"] or (
            f"Without {candidate['precise_missing_evidence'].rstrip('.')}, the collection cannot resolve "
            f"the mapped question about {candidate['topic']}."
        )
        candidate["contribution"] = candidate["contribution"] or (
            f"Evidence that supplies {candidate['evidence_needed'].rstrip('.')} would fill the identified collection-level omission."
        )
        candidate["specificity_errors"] = _gap_specificity_errors(candidate)
        candidate["specificity_status"] = (
            "qualified" if not candidate["specificity_errors"] else "underspecified_gap"
        )
        result.append(candidate)
    return sorted(result, key=lambda row: row["gap_id"])


def _related_clusters_for_gap(
    signal: Mapping[str, Any],
    profile: Mapping[str, Any],
    clusters_by_source: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[str]:
    candidates = list(
        clusters_by_source.get(str(profile.get("source_id") or ""), []) or []
    )
    if not candidates:
        return []
    terms = _tokens(
        signal.get("topic")
        or signal.get("precise_missing_evidence")
        or signal.get("missing_evidence")
        or signal.get("gap_text")
        or ""
    )
    ranked = [
        (
            len(
                terms
                & _tokens(
                    [cluster.get("semantic_identity", ""), cluster.get("label", "")]
                )
            ),
            str(cluster.get("cluster_id") or ""),
        )
        for cluster in candidates
    ]
    best = max((score for score, _ in ranked), default=0)
    if best > 0:
        return sorted(
            cluster_id for score, cluster_id in ranked if score == best and cluster_id
        )
    return [str(candidates[0].get("cluster_id") or "")] if len(candidates) == 1 else []


_VAGUE_GAP = re.compile(
    r"^(?:more|further|additional) research(?: is needed)?$|"
    r"^unobserved factors?(?: affecting .*)?$|"
    r"^(?:limited|insufficient|more) data$|"
    r"^need for (?:comparative )?analysis$|"
    r"^future studies$",
    flags=re.IGNORECASE,
)


def _gap_specificity_errors(candidate: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    topic = str(candidate.get("topic") or "").strip()
    missing = str(candidate.get("precise_missing_evidence") or "").strip()
    if len(_tokens(topic)) < 2 and not candidate.get("related_cluster_ids"):
        errors.append("topic_not_specific")
    if len(_tokens(missing)) < 4:
        errors.append("missing_evidence_not_specific")
    if _VAGUE_GAP.fullmatch(missing.strip(" .")):
        errors.append("generic_gap_language")
    if not candidate.get("related_cluster_ids") and not candidate.get(
        "collection_level_rationale"
    ):
        errors.append("missing_cluster_or_collection_rationale")
    if not candidate.get("proposition_ids"):
        errors.append("missing_originating_proposition")
    if candidate.get("related_cluster_ids") and not candidate.get(
        "originating_cluster_revisions"
    ):
        errors.append("missing_originating_cluster_revision")
    if not _as_mapping(candidate.get("missing_cell")).get("description"):
        errors.append("missing_precise_matrix_cell")
    evidence = list(candidate.get("supporting_evidence", []) or [])
    if not evidence or not all(
        row.get("source_id")
        and (row.get("evidence_anchor_id") or row.get("claim_id"))
        and _complete_locator(row.get("locator"))
        for row in evidence
    ):
        errors.append("missing_locator_backed_generation_evidence")
    rule = str(candidate.get("rule") or "")
    if (
        rule == "cross_cluster_integration"
        and len(set(candidate.get("related_cluster_ids", []) or [])) < 2
    ):
        errors.append("cross_cluster_gap_requires_two_clusters")
    if (
        rule == "contradictory_findings"
        and len(
            {
                evidence_base_id
                for row in evidence
                if (evidence_base_id := _reference_evidence_base_id(row))
            }
        )
        < 2
    ):
        errors.append("contradiction_requires_two_effective_evidence_bases")
    return sorted(set(errors))


def _gap_rule_admission_errors(
    candidate: Mapping[str, Any],
    complete_support: Sequence[Mapping[str, Any]],
    claim_lookup: Mapping[tuple[str, str], Mapping[str, Any]],
) -> list[str]:
    """Enforce rule-specific evidence relationships before promotion.

    Generic source and locator thresholds cannot establish a contradiction:
    the cited claims must describe the same proposition, come from distinct
    study families, and point in different substantive directions.
    """

    rule = str(candidate.get("rule") or "")
    if rule != "contradictory_findings":
        return []
    claims = [
        claim
        for reference in complete_support
        if (
            claim := claim_lookup.get(
                (
                    str(reference.get("source_id") or ""),
                    str(reference.get("claim_id") or ""),
                )
            )
        )
        is not None
        and str(claim.get("direction") or "not_reported")
        not in {"not_reported", "mixed"}
    ]
    lineage_keys = {
        (
            str(row.get("source_id") or ""),
            str(row.get("evidence_anchor_id") or row.get("claim_id") or ""),
        )
        for row in candidate.get("proposition_evidence_keys", []) or []
        if isinstance(row, Mapping)
    }
    named_proposition = bool(candidate.get("proposition_id")) and bool(lineage_keys)
    opposing_comparable_pair = any(
        bool(_anchor_evidence_base_id(left))
        and bool(_anchor_evidence_base_id(right))
        and _anchor_evidence_base_id(left) != _anchor_evidence_base_id(right)
        and str(left.get("direction")) != str(right.get("direction"))
        and (
            _same_semantic_proposition(left, right)
            or (
                named_proposition
                and (
                    str(left.get("source_id") or ""),
                    str(left.get("evidence_anchor_id") or left.get("claim_id") or ""),
                )
                in lineage_keys
                and (
                    str(right.get("source_id") or ""),
                    str(right.get("evidence_anchor_id") or right.get("claim_id") or ""),
                )
                in lineage_keys
            )
        )
        for left, right in combinations(claims, 2)
    )
    return (
        []
        if opposing_comparable_pair
        else ["contradiction_requires_opposing_comparable_claims"]
    )


def _gap_search_terms(candidate: Mapping[str, Any]) -> list[str]:
    terms = _tokens(
        [
            candidate.get("rule", ""),
            candidate.get("topic", ""),
            candidate.get("precise_missing_evidence", ""),
        ]
    )
    generic = {
        "author",
        "boundary",
        "but",
        "condition",
        "contradictory",
        "coverage",
        "cross",
        "data",
        "empirical",
        "finding",
        "gap",
        "integration",
        "mechanism",
        "measurement",
        "methodological",
        "replication",
        "stated",
        "untested",
    }
    return sorted(term for term in terms if term not in generic and len(term) > 2)


def _strict_gap_adjudication(
    gap: Mapping[str, Any],
    *,
    min_families: int,
) -> dict[str, Any]:
    """Explain the strong-gap gate without delegating its decision to a model."""

    rule_result = _as_mapping((gap.get("rule_results") or [{}])[0])
    value = _as_mapping(gap.get("value_assessment"))
    resolution = _as_mapping(gap.get("resolution_path"))
    specificity_passed = not bool(gap.get("specificity_errors"))
    rule_passed = bool(rule_result.get("rule_specific_admission_passed"))
    support_count = int(rule_result.get("effective_evidence_base_count", 0) or 0)
    locator_completeness = float(rule_result.get("locator_completeness", 0) or 0)
    search_complete = bool(rule_result.get("collection_search_complete"))
    full_answers = int(rule_result.get("answered_elsewhere_count", 0) or 0)
    partial_answers = int(rule_result.get("partially_answered_elsewhere_count", 0) or 0)
    non_obvious = bool(value.get("non_obviousness_passed"))
    important = bool(value.get("importance_passed"))
    path_type = str(resolution.get("path_type") or "")
    requirements = _as_mapping(resolution.get("requirements"))
    resolution_present = bool(
        path_type in _RESOLUTION_PATH_REQUIREMENTS
        and resolution.get("question")
        and resolution.get("evidence_needed")
        and all(
            _flatten_values(requirements.get(field_name))
            for field_name in _RESOLUTION_PATH_REQUIREMENTS.get(path_type, ())
        )
        and len(_tokens(resolution.get("feasibility", ""))) >= 2
    )
    checks = [
        {
            "requirement": "The candidate is bounded and originates in a named proposition or matrix cell.",
            "passed": specificity_passed,
            "explanation": (
                "The candidate has bounded proposition, cluster-revision, matrix-cell, and located evidence lineage."
                if specificity_passed
                else "One or more specificity or proposition-lineage requirements are missing."
            ),
        },
        {
            "requirement": "The declared gap rule passes its relationship-specific evidence test.",
            "passed": rule_passed,
            "explanation": (
                f"The {gap.get('rule', '')} rule passed."
                if rule_passed
                else "The cited evidence does not establish the precise relationship required by this rule."
            ),
        },
        {
            "requirement": f"At least {min_families} independent evidence bases reveal the unresolved issue.",
            "passed": support_count >= min_families,
            "explanation": "The candidate has "
            + _counted_noun(support_count, "qualifying independent evidence base")
            + ".",
        },
        {
            "requirement": "All promotion evidence has complete source-native locators.",
            "passed": locator_completeness == 1.0,
            "explanation": f"Locator completeness is {locator_completeness:.0%}.",
        },
        {
            "requirement": "The complete frozen collection neither answers nor materially narrows the candidate.",
            "passed": search_complete and full_answers == 0 and partial_answers == 0,
            "explanation": (
                f"The internal search found {full_answers} full answer(s) and {partial_answers} partial answer(s)."
                if search_complete
                else "The collection-wide internal search is incomplete."
            ),
        },
        {
            "requirement": "The puzzle is non-obvious and consequential, not merely an untested variable or routine extension.",
            "passed": non_obvious and important,
            "explanation": (
                "The value assessment passed both non-obviousness and importance."
                if non_obvious and important
                else "The reasoned value assessment did not pass both non-obviousness and importance."
            ),
        },
        {
            "requirement": "A feasible type-sensitive path could produce discriminating evidence.",
            "passed": resolution_present,
            "explanation": (
                f"A {str(resolution.get('path_type') or '').replace('_', ' ')} resolution path is specified."
                if resolution_present
                else "No sufficiently specific resolution path has passed validation."
            ),
        },
    ]
    established = all(bool(check["passed"]) for check in checks)
    failed = [
        str(check["requirement"]).rstrip(" .;")
        for check in checks
        if not check["passed"]
    ]
    return {
        "kind": "strong_gap",
        "candidate": str(
            gap.get("gap_statement")
            or gap.get("precise_missing_evidence")
            or gap.get("title")
            or "Collection-relative gap"
        ),
        "decision": "established" if established else "not_established",
        "checks": checks,
        "explanation": (
            "The candidate survives the strict collection-native strong-gap threshold."
            if established
            else "The candidate remains informative, but it does not meet the strong-gap threshold because: "
            + "; ".join(failed)
            + "."
        ),
        "what_would_change": (
            "No additional collection evidence is required for this classification."
            if established
            else "The failed checks above identify the additional comparison, locator coverage, collection search result, value argument, or resolution path needed."
        ),
        "proposition_ids": [
            str(value) for value in gap.get("proposition_ids", []) or [] if str(value)
        ],
        "related_cluster_ids": [
            str(value)
            for value in gap.get("related_cluster_ids", []) or []
            if str(value)
        ],
        "evidence": [
            dict(row)
            for row in gap.get("supporting_evidence", []) or []
            if isinstance(row, Mapping)
        ],
    }


def _gap_subject_terms(candidate: Mapping[str, Any]) -> set[str]:
    return _tokens(
        [candidate.get("topic", ""), candidate.get("precise_missing_evidence", "")]
    )


def _answer_matches(
    candidate: Mapping[str, Any], answer: Any
) -> tuple[str, dict[str, Any] | None]:
    item = _as_mapping(answer) if not isinstance(answer, str) else {"text": answer}
    rule = str(item.get("rule") or item.get("gap_rule") or "")
    answer_tokens = _tokens(
        [item.get("topic", ""), item.get("text", ""), item.get("answer", "")]
    )
    candidate_tokens = _gap_subject_terms(candidate)
    if item.get("gap_id") and str(item.get("gap_id")) == str(candidate.get("gap_id")):
        subject_match = True
    else:
        subject_match = bool(candidate_tokens & answer_tokens)
    if rule and rule != candidate.get("rule"):
        return "none", None
    if not subject_match:
        return "none", None
    status = str(
        item.get("status") or item.get("answer_status") or "answered"
    ).casefold()
    if status in {"narrows", "narrowed"}:
        match = "narrows"
    elif status in {"counter", "counters", "contradicts"}:
        match = "counters"
    elif status in {"partial", "partially_answered"}:
        match = "partial"
    else:
        match = "answered"
    reference = {
        "evidence_anchor_id": str(
            item.get("evidence_anchor_id") or item.get("claim_id") or ""
        ),
        "claim_id": str(item.get("evidence_anchor_id") or item.get("claim_id") or ""),
        "source_id": str(item.get("source_id") or ""),
        "study_family_id": str(
            item.get("study_family_id") or item.get("source_id") or ""
        ),
        "locator": _locator_text(item.get("locator")),
    }
    return match, reference


def _profile_locator_completeness(profile: Mapping[str, Any]) -> float:
    claims = profile.get("claims", []) or []
    if not claims:
        return 0.0
    return sum(1 for claim in claims if claim.get("locator_complete")) / len(claims)


def semantic_closest_prior(
    candidate: Mapping[str, Any],
    profiles: Sequence[Any],
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Rank closest prior sources by deterministic semantic overlap and locator quality."""
    rows = [row for row in _ensure_profiles(profiles) if row["analytical"]]
    terms = set(_gap_search_terms(candidate))
    family_counts = Counter(row["study_family_id"] for row in rows)
    ranked: list[dict[str, Any]] = []
    for profile in rows:
        profile_terms = set(profile["search_tokens"])
        overlap = sorted(terms & profile_terms)
        if not overlap:
            continue
        union = terms | profile_terms
        semantic_score = len(overlap) / max(1, len(union))
        locator_completeness = _profile_locator_completeness(profile)
        confidence = min(
            0.99, 0.2 + semantic_score * 0.65 + locator_completeness * 0.15
        )
        ranked.append(
            {
                "prior_id": f"prior-{_stable_hash(profile['source_id'])[:12]}",
                "source_id": profile["source_id"],
                "note_id": profile["note_id"],
                "title": profile["title"],
                "note_path": profile["note_path"],
                "study_family_id": profile["study_family_id"],
                "source_count": family_counts[profile["study_family_id"]],
                "locator_completeness": round(locator_completeness, 3),
                "confidence": round(confidence, 3),
                "semantic_overlap": overlap,
                "overlap_explanation": f"Matched collection terms: {', '.join(overlap)}.",
            }
        )
    ranked.sort(
        key=lambda row: (
            -row["confidence"],
            -row["locator_completeness"],
            -row["source_count"],
            row["prior_id"],
        )
    )
    return ranked[: max(0, limit)]


def search_and_validate_gaps(
    candidates: Sequence[Mapping[str, Any]],
    profiles: Sequence[Any],
    *,
    policy: Any = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Search every analytical profile before deterministic promotion or rejection."""
    rows = _ensure_profiles(profiles)
    analytical = [row for row in rows if row["analytical"]]
    limited = [row for row in rows if row["limited"]]
    claim_lookup = {
        (str(claim["source_id"]), str(claim["claim_id"])): claim
        for row in rows
        for claim in row.get("claims", []) or []
    }
    min_families = max(
        2,
        int(
            _policy_value(
                policy, ("min_gap_support_families", "gap_promotion_min_sources"), 2
            )
        ),
    )
    prior_limit = int(_policy_value(policy, "closest_prior_limit", 5))
    validated: list[dict[str, Any]] = []
    search_log: list[dict[str, Any]] = []
    for raw_candidate in sorted(candidates, key=lambda row: str(row.get("gap_id"))):
        candidate = dict(raw_candidate)
        terms = set(_gap_search_terms(candidate))
        supporting_source_ids = {
            str(row.get("source_id"))
            for row in candidate.get("supporting_evidence", []) or []
        }
        results: list[dict[str, Any]] = []
        full_answers: list[dict[str, Any]] = []
        partial_answers: list[dict[str, Any]] = []
        countervailing: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []

        for profile in analytical:
            overlap = sorted(terms & set(profile["search_tokens"]))
            answer_status = "none"
            answer_reference: dict[str, Any] | None = None
            for answer in profile.get("gap_answers", []) or []:
                status, reference = _answer_matches(candidate, answer)
                if status != "none":
                    answer_status, answer_reference = status, reference
                    break
            if answer_status == "none":
                for claim in profile.get("claims", []) or []:
                    if not claim.get("addresses_gap"):
                        continue
                    if claim.get("gap_rule") and claim.get("gap_rule") != candidate.get(
                        "rule"
                    ):
                        continue
                    claim_terms = _tokens(
                        [
                            claim.get("topic", ""),
                            claim.get("text", ""),
                            claim.get("dimensions", {}),
                        ]
                    )
                    if not (_gap_subject_terms(candidate) & claim_terms):
                        continue
                    answer_status = (
                        "partial"
                        if claim.get("answer_status")
                        in {"partial", "partially_answered"}
                        else "answered"
                    )
                    answer_reference = _evidence_ref(claim)
                    break
            if answer_reference is not None:
                answer_reference = {
                    **answer_reference,
                    "source_id": profile["source_id"],
                    "study_family_id": profile["study_family_id"],
                }
                countervailing.append(answer_reference)
                if _complete_locator(answer_reference.get("locator")):
                    (
                        full_answers if answer_status == "answered" else partial_answers
                    ).append(answer_reference)
                else:
                    warnings.append(
                        {
                            "warning": "possible_answer_requires_locator",
                            "source_id": profile["source_id"],
                        }
                    )
            if answer_status == "answered":
                result_status = (
                    "answers"
                    if _complete_locator((answer_reference or {}).get("locator"))
                    else "full_text_required"
                )
            elif answer_status == "partial":
                result_status = (
                    "partially_answers"
                    if _complete_locator((answer_reference or {}).get("locator"))
                    else "full_text_required"
                )
            elif answer_status == "narrows":
                result_status = (
                    "narrows"
                    if _complete_locator((answer_reference or {}).get("locator"))
                    else "full_text_required"
                )
            elif answer_status == "counters":
                result_status = (
                    "counters"
                    if _complete_locator((answer_reference or {}).get("locator"))
                    else "full_text_required"
                )
            elif profile["source_id"] in supporting_source_ids:
                result_status = "originating_support"
            elif overlap:
                result_status = "related_but_nonresponsive"
            else:
                result_status = "no_match"
            results.append(
                {
                    "source_id": profile["source_id"],
                    "study_family_id": profile["study_family_id"],
                    "status": result_status,
                    "semantic_overlap": overlap,
                }
            )

        for profile in limited:
            overlap = sorted(terms & set(profile["search_tokens"]))
            if not overlap:
                continue
            warning = {
                "warning": "possible_counterevidence_requires_full_text",
                "source_id": profile["source_id"],
                "note_status": profile["note_status"],
                "semantic_overlap": overlap,
            }
            warnings.append(warning)
            results.append(
                {
                    "source_id": profile["source_id"],
                    "study_family_id": profile["study_family_id"],
                    "status": "full_text_required",
                    "semantic_overlap": overlap,
                }
            )

        analytical_source_ids = {row["source_id"] for row in analytical}
        raw_support = list(candidate.get("supporting_evidence", []) or [])
        support = [
            row
            for row in raw_support
            if str(row.get("source_id")) in analytical_source_ids
        ]
        for row in raw_support:
            if str(row.get("source_id")) not in analytical_source_ids:
                warnings.append(
                    {
                        "warning": "possible_counterevidence_requires_full_text",
                        "source_id": str(row.get("source_id") or ""),
                    }
                )
        complete_support = [
            row
            for row in support
            if _complete_locator(row.get("locator"))
            and row.get("claim_id")
            and row.get("source_id")
        ]
        support_families = {
            evidence_base_id
            for row in complete_support
            if (evidence_base_id := _reference_evidence_base_id(row))
        }
        locator_completeness = len(complete_support) / len(support) if support else 0.0
        rule_admission_errors = _gap_rule_admission_errors(
            candidate, complete_support, claim_lookup
        )
        if rule_admission_errors:
            status = "underspecified_gap"
            promoted = False
            decision = "reject_rule_admission"
        elif full_answers:
            status = "answered_within_collection"
            promoted = False
            decision = "reject"
        elif partial_answers:
            status = "narrowed_by_collection"
            promoted = False
            decision = "narrow"
        elif (
            bool(_policy_value(policy, "auto_promote_gaps", True))
            and len(support_families) >= min_families
            and len(complete_support) >= min_families
            and locator_completeness == 1.0
            and not rule_admission_errors
        ):
            status = "collection_surviving_gap"
            promoted = True
            decision = "promote"
        else:
            status = "collection_gap_lead"
            promoted = False
            decision = "retain_lead"
        rule_result = {
            "rule": candidate["rule"],
            "candidate_valid": candidate["rule"] in GAP_RULES
            and not rule_admission_errors,
            "rule_specific_admission_passed": not rule_admission_errors,
            "rule_admission_errors": rule_admission_errors,
            "independent_supporting_sources": len(
                {row.get("source_id") for row in complete_support}
            ),
            "independent_study_families": len(support_families),
            "effective_evidence_base_count": len(support_families),
            "complete_locator_count": len(complete_support),
            "locator_completeness": round(locator_completeness, 3),
            "collection_search_complete": True,
            "analytical_profile_count_searched": len(analytical),
            "answered_elsewhere_count": len(full_answers),
            "partially_answered_elsewhere_count": len(partial_answers),
            "decision": decision,
        }
        closest = semantic_closest_prior(candidate, analytical, limit=prior_limit)
        candidate.update(
            {
                "status": status,
                "scope": "collection_only",
                "promoted": promoted,
                "automation_status": "promoted"
                if promoted
                else ("rejected" if decision == "reject" else "lead"),
                "novelty_claimed": False,
                "rule_results": [rule_result],
                "supporting_evidence": support,
                "observed_evidence": support,
                "countervailing_evidence": sorted(
                    countervailing,
                    key=lambda row: (row["source_id"], row.get("claim_id", "")),
                ),
                "internal_search_terms": sorted(terms),
                "internal_search_results": results,
                "closest_prior_work": closest,
                "warnings": sorted(
                    warnings, key=lambda row: (row["warning"], row["source_id"])
                ),
                "promotion_metadata": {
                    "scope": "collection_only",
                    "promoted": promoted,
                    "novelty_claimed": False,
                    "rule_results": [rule_result],
                    "precise_missing_evidence": candidate["precise_missing_evidence"],
                    "supporting_locators": [
                        row for row in support if row.get("locator")
                    ],
                    "countervailing_locators": [
                        row for row in countervailing if row.get("locator")
                    ],
                    "internal_search": {"terms": sorted(terms), "results": results},
                    "why_matters": candidate["why_matters"],
                    "contribution": candidate["contribution"],
                },
            }
        )
        candidate["strict_adjudication"] = _strict_gap_adjudication(
            candidate,
            min_families=min_families,
        )
        validated.append(candidate)
        search_log.append(
            {
                "search_id": f"search-{_stable_hash(candidate['gap_id'])[:12]}",
                "gap_id": candidate["gap_id"],
                "terms": sorted(terms),
                "analytical_profile_count_searched": len(analytical),
                "results": results,
                "limited_profile_warnings": [
                    row
                    for row in warnings
                    if row["warning"] == "possible_counterevidence_requires_full_text"
                ],
                "complete": True,
            }
        )
    return validated, search_log


def rank_gap_registry(gaps: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Deterministic ordering by decision, confidence, source count, locators, and stable ID."""
    status_order = {
        "collection_surviving_gap": 0,
        "collection_gap_lead": 1,
        "narrowed_by_collection": 2,
        "answered_within_collection": 3,
        "underspecified_gap": 4,
    }
    rows: list[dict[str, Any]] = []
    for gap in gaps:
        row = dict(gap)
        result = (row.get("rule_results") or [{}])[0]
        assessment = _as_mapping(row.get("value_assessment"))
        resolution = _as_mapping(row.get("resolution_path"))
        requirements = _as_mapping(resolution.get("requirements"))
        information_gain = str(assessment.get("information_gain") or "low")
        path_type = str(resolution.get("path_type") or "")
        required_fields = _RESOLUTION_PATH_REQUIREMENTS.get(path_type, ())
        required_complete = sum(
            bool(_flatten_values(requirements.get(field))) for field in required_fields
        )
        resolution_completeness = (
            required_complete
            + bool(resolution.get("question"))
            + bool(resolution.get("evidence_needed"))
        ) / max(1, len(required_fields) + 2)
        closest_confidence = max(
            (
                float(item.get("confidence", 0))
                for item in row.get("closest_prior_work", []) or []
            ),
            default=0.0,
        )
        source_count = int(result.get("independent_study_families", 0))
        locator_completeness = float(result.get("locator_completeness", 0))
        confidence = min(
            0.99,
            0.35
            + 0.12 * min(source_count, 4)
            + 0.25 * locator_completeness
            + 0.1 * closest_confidence,
        )
        confidence_tier = (
            "high"
            if confidence >= 0.8
            else ("moderate" if confidence >= 0.6 else "low")
        )
        row["ranking"] = {
            "confidence": round(confidence, 3),
            "confidence_tier": confidence_tier,
            "source_count": source_count,
            "locator_completeness": round(locator_completeness, 3),
            "stable_id": row["gap_id"],
            "information_gain": information_gain,
            "resolution_path_completeness": round(resolution_completeness, 3),
            "design_completeness": round(resolution_completeness, 3),
        }
        rows.append(row)
    rows.sort(
        key=lambda row: (
            status_order.get(str(row.get("status")), 9),
            {"high": 0, "moderate": 1, "low": 2}.get(
                row["ranking"]["information_gain"], 3
            ),
            -row["ranking"]["resolution_path_completeness"],
            -row["ranking"]["source_count"],
            -row["ranking"]["locator_completeness"],
            row["gap_id"],
        )
    )
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return rows


def _reasoner_stage(
    reasoner: Any,
    reasoner_call: Any,
    *,
    stage: str,
    key: str,
    method_name: str,
    profiles: Sequence[Any],
    request: Any,
    context: Mapping[str, Any],
) -> Mapping[str, Any]:
    if reasoner is None:
        return {}
    if reasoner_call is not None:
        try:
            return reasoner_call(stage, key, method_name, profiles, context)
        except LiteratureSynthesisPartialError:
            raise
        except Exception:
            # A paid, checkpointed synthesis failure is resumable work, not
            # evidence that the deterministic fallback is an equivalent
            # completed result. Let the pipeline mark the invocation partial.
            raise
    if isinstance(reasoner, Mapping):
        if stage == "cluster_proposal":
            return {
                "clusters": list(
                    reasoner.get("cluster_proposals") or reasoner.get("clusters") or []
                )
            }
        if stage == "cluster_synthesis":
            syntheses = reasoner.get("cluster_syntheses", {})
            return (
                dict(syntheses.get(key, {})) if isinstance(syntheses, Mapping) else {}
            )
        if stage == "gap_adjudication":
            return {
                "gaps": list(
                    reasoner.get("gap_rationales") or reasoner.get("gaps") or []
                ),
                "rejected": list(
                    reasoner.get("rejected_gap_rationales")
                    or reasoner.get("rejected")
                    or []
                ),
            }
        return {}
    method = getattr(reasoner, method_name, None)
    if not callable(method) or request is None:
        return {}
    try:
        value = method(profiles, request, context=context)
    except LiteratureSynthesisPartialError:
        raise
    except Exception:
        return {}
    return _as_mapping(value) if value else {}


def _cluster_item_text(row: Mapping[str, Any]) -> str:
    for key in (
        "assertion",
        "technical_finding",
        "finding",
        "claim",
        "proposition",
        "position",
        "agreement",
        "contradiction",
        "boundary",
        "fault_line",
        "methodological_fault_line",
        # Evidence threads carry their substantive synthesis in summary.
        # relationship is only a machine classification such as
        # "complementary" and must never displace that prose in a verdict.
        "summary",
        "relationship",
        "role",
        "text",
    ):
        text = str(row.get(key) or "").strip()
        if text:
            return text
    return ""


def _gap_anchor_catalog(
    cluster_syntheses: Mapping[str, Mapping[str, Any]],
) -> dict[tuple[str, str, str], dict[str, Any]]:
    catalog: dict[tuple[str, str, str], dict[str, Any]] = {}
    for cluster_id, synthesis in cluster_syntheses.items():
        for section in CLUSTER_SYNTHESIS_SECTIONS:
            for row in synthesis.get(section, []) or []:
                if not isinstance(row, Mapping) or not row.get("item_id"):
                    continue
                catalog[(str(cluster_id), section, str(row["item_id"]))] = dict(row)
    return catalog


def _resolve_gap_anchors(
    gap: Mapping[str, Any],
    proposed_anchors: Sequence[Mapping[str, Any]],
    cluster_syntheses: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, str]]:
    """Validate supplied anchors and deterministically derive missing cluster anchors."""

    catalog = _gap_anchor_catalog(cluster_syntheses)
    related = {str(value) for value in gap.get("related_cluster_ids", []) or []}
    support_keys = {
        (str(row.get("source_id") or ""), str(row.get("claim_id") or ""))
        for row in gap.get("supporting_evidence", []) or []
    }
    accepted: dict[tuple[str, str, str], dict[str, str]] = {}
    for raw in proposed_anchors:
        anchor = _as_mapping(raw)
        key = (
            str(anchor.get("cluster_id") or ""),
            str(anchor.get("section") or ""),
            str(anchor.get("item_id") or ""),
        )
        row = catalog.get(key)
        if row is None or key[0] not in related:
            continue
        row_evidence = {
            (str(ref.get("source_id") or ""), str(ref.get("claim_id") or ""))
            for ref in row.get("evidence", []) or []
        }
        if support_keys and not (row_evidence & support_keys):
            continue
        accepted[key] = {"cluster_id": key[0], "section": key[1], "item_id": key[2]}

    gap_terms = _tokens(
        [
            gap.get("gap_statement", ""),
            gap.get("precise_missing_evidence", ""),
            gap.get("observed_pattern", ""),
        ]
    )
    preferred_sections = {
        "contradictory_findings": ("contradictions", "positions"),
        "untested_mechanism": ("central_findings", "positions"),
        "empirical_coverage": ("boundary_conditions", "central_findings"),
        "methodological_concentration": ("methodological_fault_lines",),
        "measurement_or_data": ("methodological_fault_lines",),
        "boundary_condition": ("boundary_conditions", "contradictions"),
        "replication": ("central_findings", "agreements"),
        "cross_cluster_integration": ("related_clusters",),
        "author_stated_gap": ("central_findings", "source_roles"),
    }.get(str(gap.get("rule") or ""), ())
    anchored_clusters = {row["cluster_id"] for row in accepted.values()}
    for cluster_id in sorted(related - anchored_clusters):
        candidates: list[tuple[int, str, str, Mapping[str, Any]]] = []
        for (candidate_cluster, section, item_id), row in catalog.items():
            if candidate_cluster != cluster_id:
                continue
            evidence_keys = {
                (str(ref.get("source_id") or ""), str(ref.get("claim_id") or ""))
                for ref in row.get("evidence", []) or []
            }
            evidence_overlap = len(evidence_keys & support_keys)
            if support_keys and evidence_overlap == 0:
                continue
            semantic_overlap = len(gap_terms & _tokens(_cluster_item_text(row)))
            preference = (
                len(preferred_sections) - preferred_sections.index(section)
                if section in preferred_sections
                else 0
            )
            candidates.append(
                (
                    100 * evidence_overlap + 20 * preference + semantic_overlap,
                    section,
                    item_id,
                    row,
                )
            )
        if candidates:
            _, section, item_id, _ = max(
                candidates, key=lambda value: (value[0], value[1], value[2])
            )
            key = (cluster_id, section, item_id)
            accepted[key] = {
                "cluster_id": cluster_id,
                "section": section,
                "item_id": item_id,
            }
    return sorted(
        accepted.values(),
        key=lambda row: (row["cluster_id"], row["section"], row["item_id"]),
    )


_RESOLUTION_PATH_REQUIREMENTS: Mapping[str, tuple[str, ...]] = {
    "quantitative": ("estimand", "comparison", "identification", "measurement"),
    "qualitative": (
        "case_selection",
        "mechanism_evidence",
        "negative_cases",
        "process_observations",
    ),
    "historical_interpretive": (
        "archives",
        "periodization",
        "source_criticism",
        "competing_interpretations",
    ),
    "theoretical": ("premises", "derivation", "scope", "model_comparison"),
    "normative": ("principles", "objections", "application_tests"),
    "methodological": ("assumptions", "diagnostics", "benchmarks", "robustness"),
    "practitioner": ("implementation_evidence", "institutional_context", "bias_checks"),
}


def _legacy_design_resolution_path(design: Mapping[str, Any]) -> dict[str, Any]:
    if not design:
        return {}
    design_type = str(design.get("design_type") or "").casefold()
    if any(
        token in design_type
        for token in ("qualitative", "case", "process", "interview", "ethnograph")
    ):
        path_type = "qualitative"
        requirements = {
            "case_selection": design.get("target_population")
            or design.get("unit_of_analysis"),
            "mechanism_evidence": design.get("mechanism_measures"),
            "negative_cases": design.get("comparator"),
            "process_observations": design.get("falsification_or_process_tests"),
        }
    elif any(token in design_type for token in ("histor", "archive", "interpret")):
        path_type = "historical_interpretive"
        requirements = {
            "archives": design.get("data_route"),
            "periodization": design.get("target_population"),
            "source_criticism": design.get("validity_risks"),
            "competing_interpretations": design.get(
                "confounders_or_rival_explanations"
            ),
        }
    elif "normative" in design_type:
        path_type = "normative"
        requirements = {
            "principles": design.get("exposure_or_treatment"),
            "objections": design.get("confounders_or_rival_explanations"),
            "application_tests": design.get("falsification_or_process_tests"),
        }
    elif any(token in design_type for token in ("theor", "model")):
        path_type = "theoretical"
        requirements = {
            "premises": design.get("exposure_or_treatment"),
            "derivation": design.get("identification_or_inference_strategy"),
            "scope": design.get("target_population"),
            "model_comparison": design.get("comparator"),
        }
    elif any(token in design_type for token in ("method", "benchmark", "robust")):
        path_type = "methodological"
        requirements = {
            "assumptions": design.get("confounders_or_rival_explanations"),
            "diagnostics": design.get("falsification_or_process_tests"),
            "benchmarks": design.get("comparator"),
            "robustness": design.get("validity_risks"),
        }
    elif any(token in design_type for token in ("pract", "implement")):
        path_type = "practitioner"
        requirements = {
            "implementation_evidence": design.get("data_route"),
            "institutional_context": design.get("target_population"),
            "bias_checks": design.get("validity_risks"),
        }
    else:
        path_type = "quantitative"
        requirements = {
            "estimand": design.get("estimand"),
            "comparison": design.get("comparator"),
            "identification": design.get("identification_or_inference_strategy"),
            "measurement": [
                *(design.get("outcomes", []) or []),
                *(design.get("mechanism_measures", []) or []),
            ],
        }
    return {
        "path_type": path_type,
        "question": str(design.get("research_question") or ""),
        "evidence_needed": str(design.get("data_route") or ""),
        "requirements": requirements,
        "feasibility": str(design.get("feasibility") or ""),
        "limitations": list(design.get("validity_risks", []) or []),
    }


def _gap_quality_errors(gap: Mapping[str, Any], *, require_design: bool) -> list[str]:
    assessment = _as_mapping(gap.get("value_assessment"))
    resolution_path = _as_mapping(gap.get("resolution_path"))
    errors: list[str] = []
    if len(_tokens(assessment.get("puzzle_type", ""))) < 1:
        errors.append("missing_value_assessment_puzzle_type")
    assessment_text_fields = (
        "puzzle",
        "strongest_obvious_answer",
        "why_obvious_answer_is_inadequate",
        "decision_or_inference_changed",
    )
    for field_name in assessment_text_fields:
        if len(_tokens(assessment.get(field_name, ""))) < 2:
            errors.append(f"missing_value_assessment_{field_name}")
    if not assessment.get("competing_explanations"):
        errors.append("missing_competing_explanations")
    if assessment.get("information_gain") not in {"high", "moderate"}:
        errors.append("insufficient_information_gain")
    if assessment.get("non_obviousness_passed") is not True:
        errors.append("obvious_answer_not_falsified")
    if assessment.get("importance_passed") is not True:
        errors.append("importance_not_established")
    errors.extend(
        str(value)
        for value in assessment.get("rejection_reasons", []) or []
        if str(value)
    )

    if require_design:
        path_type = str(resolution_path.get("path_type") or "")
        if path_type not in _RESOLUTION_PATH_REQUIREMENTS:
            errors.append("missing_or_invalid_resolution_path_type")
        if len(_tokens(resolution_path.get("question", ""))) < 3:
            errors.append("missing_resolution_path_question")
        if len(_tokens(resolution_path.get("evidence_needed", ""))) < 3:
            errors.append("missing_resolution_path_evidence_needed")
        requirements = _as_mapping(resolution_path.get("requirements"))
        for field_name in _RESOLUTION_PATH_REQUIREMENTS.get(path_type, ()):
            if not _flatten_values(requirements.get(field_name)):
                errors.append(f"missing_resolution_path_{field_name}")
        if len(_tokens(resolution_path.get("feasibility", ""))) < 2:
            errors.append("missing_resolution_path_feasibility")
    return sorted(set(errors))


def _normalize_checkpoint_scalar(value: Any) -> str:
    """Clean one-item arrays stringified by older provider adapters."""

    text = str(value or "").strip()
    if len(text) >= 4 and text[:2] in {"['", '["'} and text[-2:] in {"']", '"]'}:
        return text[2:-2].strip()
    return text


def _normalize_gap_nested_scalars(
    values: Mapping[str, Any], fields: Sequence[str]
) -> dict[str, Any]:
    normalized = dict(values)
    for field_name in fields:
        normalized[field_name] = _normalize_checkpoint_scalar(
            normalized.get(field_name)
        )
    return normalized


def _gap_structured_signature(gap: Mapping[str, Any]) -> str:
    resolution = _as_mapping(gap.get("resolution_path"))
    requirements = _as_mapping(resolution.get("requirements"))
    dimensions = {
        "rule": str(gap.get("rule") or ""),
        "proposition_ids": sorted(
            str(value) for value in gap.get("proposition_ids", []) or []
        ),
        "missing_cell": _as_mapping(gap.get("missing_cell")),
        "path_type": str(resolution.get("path_type") or ""),
        "requirements": requirements,
        "topic": _canonical_phrase(gap.get("topic", "")),
    }
    return _stable_hash(dimensions)


def _merge_candidates_are_compatible(
    canonical: Mapping[str, Any], candidate: Mapping[str, Any]
) -> bool:
    if str(canonical.get("rule") or "") != str(candidate.get("rule") or ""):
        return False
    canonical_topic = _tokens(canonical.get("topic", ""))
    candidate_topic = _tokens(candidate.get("topic", ""))
    if not canonical_topic or not candidate_topic:
        return False
    topic_overlap = len(canonical_topic & candidate_topic) / max(
        1, min(len(canonical_topic), len(candidate_topic))
    )
    canonical_missing = _tokens(
        [
            canonical.get("gap_statement", ""),
            canonical.get("precise_missing_evidence", ""),
        ]
    )
    candidate_missing = _tokens(candidate.get("precise_missing_evidence", ""))
    missing_overlap = len(canonical_missing & candidate_missing)
    missing_ratio = missing_overlap / max(
        1, min(len(canonical_missing), len(candidate_missing))
    )
    return topic_overlap >= 0.5 and (missing_overlap >= 3 or missing_ratio >= 0.4)


def _reframing_is_evidence_constrained(
    reframed: Mapping[str, Any], original: Mapping[str, Any]
) -> bool:
    """Allow a rule change only when both candidates concern the same evidence-backed puzzle."""

    reframed_evidence = {
        (str(row.get("source_id") or ""), str(row.get("claim_id") or ""))
        for row in reframed.get("supporting_evidence", []) or []
        if isinstance(row, Mapping)
    }
    original_evidence = {
        (str(row.get("source_id") or ""), str(row.get("claim_id") or ""))
        for row in original.get("supporting_evidence", []) or []
        if isinstance(row, Mapping)
    }
    if not (reframed_evidence & original_evidence):
        return False
    reframed_terms = _tokens(
        [
            reframed.get("topic", ""),
            reframed.get("gap_statement", ""),
            reframed.get("precise_missing_evidence", ""),
        ]
    )
    original_terms = _tokens(
        [original.get("topic", ""), original.get("precise_missing_evidence", "")]
    )
    overlap = len(reframed_terms & original_terms)
    overlap_ratio = overlap / max(1, min(len(reframed_terms), len(original_terms)))
    return overlap >= 3 or overlap_ratio >= 0.35


def _apply_gap_rationales(
    gaps: Sequence[Mapping[str, Any]],
    response: Mapping[str, Any],
    profiles: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    profile_by_source = {str(row["source_id"]): row for row in profiles}
    rationale_by_id = {
        str(row.get("gap_id") or ""): _as_mapping(row)
        for row in response.get("gaps", []) or []
        if isinstance(row, Mapping) and row.get("gap_id")
    }
    result: list[dict[str, Any]] = []
    for raw_gap in gaps:
        gap = dict(raw_gap)
        rationale = rationale_by_id.get(str(gap.get("gap_id") or ""), {})
        reasoner_evidence = _resolve_reasoner_evidence(
            rationale.get("supporting_evidence", []),
            profile_by_source,
        )
        reasoner_evidence = [
            reference
            for reference in reasoner_evidence
            if (
                (
                    profile := profile_by_source.get(
                        str(reference.get("source_id") or "")
                    )
                )
                is not None
                and (
                    claim := next(
                        (
                            item
                            for item in profile.get("claims", []) or []
                            if str(item.get("claim_id") or "")
                            == str(reference.get("claim_id") or "")
                        ),
                        None,
                    )
                )
                is not None
                and _gap_support_is_relevant(gap, claim)
            )
        ]
        if rationale and reasoner_evidence:
            for field in (
                "title",
                "gap_statement",
                "precise_missing_evidence",
                "generation_explanation",
                "observed_pattern",
                "internal_search_summary",
                "closest_prior_explanation",
                "decision_reasoning",
                "evidence_needed",
                "why_matters",
                "contribution",
                "confidence",
                "priority_tier",
            ):
                proposed_text = str(rationale.get(field) or "").strip()
                if not proposed_text:
                    continue
                if field in {"title", "gap_statement"} and (
                    len(_tokens(proposed_text)) < 2
                    or _VAGUE_GAP.fullmatch(proposed_text.strip(" ."))
                ):
                    continue
                gap[field] = proposed_text
            proposed_clusters = {
                str(value)
                for value in rationale.get("related_cluster_ids", []) or []
                if str(value) in set(gap.get("related_cluster_ids", []) or [])
            }
            if proposed_clusters:
                gap["related_cluster_ids"] = sorted(proposed_clusters)
            gap["value_assessment"] = _normalize_gap_nested_scalars(
                _as_mapping(rationale.get("value_assessment")),
                (
                    "puzzle_type",
                    "puzzle",
                    "strongest_obvious_answer",
                    "why_obvious_answer_is_inadequate",
                    "decision_or_inference_changed",
                ),
            )
            proposed_resolution = _as_mapping(rationale.get("resolution_path"))
            if not proposed_resolution:
                proposed_resolution = _legacy_design_resolution_path(
                    _as_mapping(rationale.get("study_design"))
                )
            gap["resolution_path"] = proposed_resolution
            # Retain a supplied legacy design only in machine audit data; the
            # canonical decision and Markdown use the type-sensitive path.
            if rationale.get("study_design"):
                gap["legacy_study_design"] = _as_mapping(rationale.get("study_design"))
            gap["proposed_anchors"] = [
                dict(row)
                for row in rationale.get("anchors", []) or []
                if isinstance(row, Mapping)
            ]
            gap["merged_from_gap_ids"] = sorted(
                {
                    str(value)
                    for value in rationale.get("merged_from_gap_ids", []) or []
                    if str(value)
                }
            )
            gap["reframed_from_gap_id"] = str(
                rationale.get("reframed_from_gap_id") or ""
            )
            reasoner_counter = _resolve_reasoner_evidence(
                rationale.get("countervailing_evidence", []),
                profile_by_source,
            )
            if reasoner_counter:
                gap["countervailing_evidence"] = sorted(
                    {
                        _stable_hash(row): row
                        for row in [
                            *(gap.get("countervailing_evidence", []) or []),
                            *reasoner_counter,
                        ]
                    }.values(),
                    key=lambda row: (row["source_id"], row["claim_id"]),
                )
            gap["rationale_status"] = "reasoned"
        else:
            gap["rationale_status"] = "deterministic_fallback"
        rule_result = (gap.get("rule_results") or [{}])[0]
        searched = int(rule_result.get("analytical_profile_count_searched", 0) or 0)
        gap.setdefault("title", str(gap.get("precise_missing_evidence") or ""))
        gap.setdefault("gap_statement", str(gap.get("precise_missing_evidence") or ""))
        gap.setdefault(
            "internal_search_summary",
            f"The mapper searched {searched} analytical profiles in the frozen collection; "
            f"{rule_result.get('answered_elsewhere_count', 0)} fully answered and "
            f"{rule_result.get('partially_answered_elsewhere_count', 0)} partially answered the candidate.",
        )
        closest = list(gap.get("closest_prior_work", []) or [])
        gap.setdefault(
            "closest_prior_explanation",
            (
                f"The closest mapped source was {closest[0].get('title', closest[0].get('source_id', ''))}: "
                f"{closest[0].get('overlap_explanation', '')}"
                if closest
                else "No analytical profile supplied close prior evidence for this candidate."
            ),
        )
        gap.setdefault(
            "decision_reasoning",
            f"The deterministic rule decision was {rule_result.get('decision', 'retain_lead')} with "
            f"{rule_result.get('independent_study_families', 0)} independent study families and "
            f"locator completeness {rule_result.get('locator_completeness', 0)}.",
        )
        gap.setdefault(
            "evidence_needed", str(gap.get("precise_missing_evidence") or "")
        )
        if not gap.get("resolution_path") and gap.get("study_design"):
            gap["resolution_path"] = _legacy_design_resolution_path(
                _as_mapping(gap.get("study_design"))
            )
        result.append(gap)
    return result


def _canonicalize_gap_status_prose(gap: dict[str, Any]) -> None:
    """Make user-facing gap language follow deterministic adjudication."""

    strict = _as_mapping(gap.get("strict_adjudication"))
    strict_decision = str(strict.get("decision") or "not_established")
    status = str(gap.get("status") or "collection_gap_lead")
    reasoning = _human_projection_text(gap.get("decision_reasoning") or "")
    if strict_decision != "established":
        reasoning = re.sub(
            r"\b(?:a\s+)?(?:strong|definitive|confirmed)\s+(?:collection\s+)?gap\b",
            "a collection gap candidate",
            reasoning,
            flags=re.I,
        )
    if status != "collection_surviving_gap":
        reasoning = re.sub(
            r"\b(?:survived|was promoted|qualifies as a mapped gap)\b",
            "remains under collection-level adjudication",
            reasoning,
            flags=re.I,
        )
    status_sentence = {
        "collection_surviving_gap": (
            "The candidate survived the collection-wide checks and is retained as a collection-scoped gap."
        ),
        "collection_gap_lead": (
            "The candidate remains a collection gap lead; the stricter strong-gap threshold was not established."
        ),
        "answered_within_collection": (
            "The candidate was answered within the frozen collection and is not a visible gap."
        ),
        "narrowed_by_collection": (
            "Collection evidence narrowed the candidate; only the bounded remainder is retained."
        ),
    }.get(status, "The candidate did not pass visible collection-gap promotion.")
    explanation = _human_projection_text(strict.get("explanation") or "")
    gap["decision_reasoning"] = " ".join(
        part for part in (status_sentence, explanation, reasoning) if part
    )


def _resolution_path_summary(resolution: Mapping[str, Any]) -> list[str]:
    """Render an evidence route without turning it into a project study design."""

    path_type = str(resolution.get("path_type") or "")
    requirements = _as_mapping(resolution.get("requirements"))
    requirement_names = [
        str(field_name).replace("_", " ")
        for field_name in _RESOLUTION_PATH_REQUIREMENTS.get(path_type, ())
        if _flatten_values(requirements.get(field_name))
    ]
    lines = [
        f"**Approach:** {path_type.replace('_', ' ')}",
        f"**Question:** {_human_projection_text(resolution.get('question') or '')}",
        f"**Evidence needed:** {_human_projection_text(resolution.get('evidence_needed') or '')}",
    ]
    if requirement_names:
        lines.append(
            "**The evidence route must specify:** "
            + ", ".join(requirement_names)
            + "."
        )
    lines.append(
        "This is a collection-grounded direction for resolving the uncertainty, not a finalized study design."
    )
    return [line for line in lines if not line.endswith(": ")]


def _apply_gap_adjudication(
    gaps: Sequence[Mapping[str, Any]],
    response: Mapping[str, Any],
    profiles: Sequence[Mapping[str, Any]],
    cluster_syntheses: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    policy: Any = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cluster_syntheses = cluster_syntheses or {}
    reasoned = _apply_gap_rationales(gaps, response, profiles)
    retained_ids = {
        str(row.get("gap_id") or "")
        for row in reasoned
        if row.get("rationale_status") == "reasoned"
    }
    gap_by_id = {str(row.get("gap_id") or ""): dict(row) for row in gaps}
    rejected_by_id: dict[str, dict[str, Any]] = {}
    for raw_rejection in response.get("rejected", []) or []:
        if not isinstance(raw_rejection, Mapping):
            continue
        gap_id = str(raw_rejection.get("gap_id") or "")
        reason = str(raw_rejection.get("reason") or "").strip()
        if (
            gap_id not in gap_by_id
            or gap_id in retained_ids
            or len(_tokens(reason)) < 2
        ):
            continue
        rejected_by_id[gap_id] = {
            **gap_by_id[gap_id],
            "status": "underspecified_gap",
            "promoted": False,
            "automation_status": "rejected",
            "adjudication_reason": reason,
            "adjudication_record": dict(raw_rejection),
        }
    visible: list[dict[str, Any]] = []
    require_design = bool(_policy_value(policy, "require_executable_gap_design", True))
    min_gap_families = max(
        2,
        int(
            _policy_value(
                policy,
                ("min_gap_support_families", "gap_promotion_min_sources"),
                2,
            )
        ),
    )
    seen_signatures: dict[str, str] = {}

    def errors_allow_visible_lead(
        gap: Mapping[str, Any], errors: Sequence[str]
    ) -> bool:
        allowed = all(
            str(error).startswith("missing_resolution_path_")
            or str(error) == "missing_or_invalid_resolution_path_type"
            for error in errors
        )
        value = _as_mapping(gap.get("value_assessment"))
        rule_result = _as_mapping((gap.get("rule_results") or [{}])[0])
        return bool(
            errors
            and allowed
            and not gap.get("specificity_errors")
            and rule_result.get("rule_specific_admission_passed") is True
            and value.get("non_obviousness_passed") is True
            and value.get("importance_passed") is True
            and value.get("information_gain") in {"high", "moderate"}
        )

    for gap in reasoned:
        gap_id = str(gap.get("gap_id") or "")
        if gap_id in rejected_by_id:
            continue
        if gap.get("rationale_status") != "reasoned":
            errors = ["gap_adjudication_did_not_retain_candidate"]
        else:
            errors = _gap_quality_errors(gap, require_design=require_design)
            reframed_from = str(gap.get("reframed_from_gap_id") or "")
            if reframed_from:
                original = gap_by_id.get(reframed_from)
                if original is None:
                    errors.append("reframing_source_candidate_not_found")
                elif reframed_from != gap_id and not _reframing_is_evidence_constrained(
                    gap, original
                ):
                    errors.append("reframing_not_evidence_constrained")
            gap["anchors"] = _resolve_gap_anchors(
                gap,
                gap.get("proposed_anchors", []) or [],
                cluster_syntheses,
            )
            gap.pop("proposed_anchors", None)
            gap["structured_signature"] = _gap_structured_signature(gap)
            prior_gap_id = seen_signatures.get(gap["structured_signature"])
            if prior_gap_id:
                errors.append(f"duplicate_structured_gap:{prior_gap_id}")
        if errors:
            quality_record = {
                **gap,
                "status": "underspecified_gap",
                "promoted": False,
                "automation_status": "rejected",
                "quality_gate_passed": False,
                "quality_rejection_reasons": sorted(set(errors)),
            }
            quality_record["strict_adjudication"] = _strict_gap_adjudication(
                quality_record,
                min_families=min_gap_families,
            )
            if errors_allow_visible_lead(quality_record, errors):
                quality_record.update(
                    status="collection_gap_lead",
                    automation_status="lead",
                    quality_warnings=quality_record["quality_rejection_reasons"],
                )
                _canonicalize_gap_status_prose(quality_record)
                visible.append(quality_record)
                continue
            rejected_by_id[gap_id] = quality_record
            continue
        seen_signatures[gap["structured_signature"]] = gap_id
        gap["quality_gate_passed"] = True
        gap["strict_adjudication"] = _strict_gap_adjudication(
            gap,
            min_families=min_gap_families,
        )
        _canonicalize_gap_status_prose(gap)
        visible.append(gap)

    merge_ledger: list[dict[str, Any]] = []
    merged_owner: dict[str, str] = {}
    for gap in visible:
        gap_id = str(gap.get("gap_id") or "")
        valid_merged_ids: list[str] = []
        for merged_id in gap.get("merged_from_gap_ids", []) or []:
            merged_id = str(merged_id)
            merged_candidate = gap_by_id.get(merged_id)
            if (
                merged_id == gap_id
                or merged_candidate is None
                or merged_id in retained_ids
                or merged_id in merged_owner
                or not _merge_candidates_are_compatible(gap, merged_candidate)
            ):
                continue
            merged_owner[merged_id] = gap_id
            valid_merged_ids.append(merged_id)
            rejected_by_id[merged_id] = {
                **merged_candidate,
                "status": "underspecified_gap",
                "promoted": False,
                "automation_status": "rejected",
                "adjudication_reason": f"Merged into {gap_id} as a semantic duplicate.",
            }
            merge_ledger.append(
                {
                    "event": "merge",
                    "canonical_gap_id": gap_id,
                    "merged_gap_id": merged_id,
                    "reason": "same structured exposure-mechanism-outcome-population-setting puzzle",
                }
            )
        gap["merged_from_gap_ids"] = sorted(valid_merged_ids)
        gap["merge_events"] = [
            row
            for row in merge_ledger
            if row["canonical_gap_id"] == str(gap.get("gap_id") or "")
        ]
    return visible, sorted(
        rejected_by_id.values(), key=lambda row: str(row.get("gap_id") or "")
    )


def _navigation_profile_rows(
    profiles: Sequence[Mapping[str, Any]],
    source_notes: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Hydrate graph-only metadata without changing analytical profiles."""

    notes_by_source = {
        str(row.get("source_id") or ""): row
        for row in source_notes
        if isinstance(row, Mapping) and row.get("source_id")
    }
    notes_by_id = {
        str(row.get("note_id") or ""): row
        for row in source_notes
        if isinstance(row, Mapping) and row.get("note_id")
    }
    hydrated: list[dict[str, Any]] = []
    for profile in profiles:
        row = dict(profile)
        note = notes_by_source.get(str(row.get("source_id") or "")) or notes_by_id.get(
            str(row.get("note_id") or "")
        )
        context = _as_mapping(row.get("context"))
        for field in (
            "original_zotero_tags",
            "normalized_tags",
            "zotero_relations",
            "citation_relations",
            "custody_relations",
        ):
            value = (note or {}).get(field)
            if value in (None, "", [], {}):
                value = context.get(field)
            if value not in (None, "", [], {}):
                row[field] = value
        if note:
            row.setdefault("zotero_item_key", note.get("zotero_item_key", ""))
            row.setdefault("note_path", note.get("note_path", ""))
            row.setdefault("title", note.get("title", ""))
        hydrated.append(row)
    return hydrated


def _source_notes_with_custody_relations(
    workspace: Path,
    source_notes: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Merge exact local custody relations into graph-only source metadata."""

    rows = [dict(row) for row in source_notes]
    by_source = {
        str(row.get("source_id") or ""): row for row in rows if row.get("source_id")
    }
    registry = workspace / "01_custody" / "source_relation_registry.csv"
    if not registry.is_file():
        return rows
    with registry.open("r", encoding="utf-8", newline="") as handle:
        relation_rows = list(csv.DictReader(handle))
    for relation in relation_rows:
        source = by_source.get(str(relation.get("source_id") or ""))
        target_id = str(relation.get("related_source_id") or "")
        if (
            source is None
            or target_id not in by_source
            or target_id == str(source.get("source_id") or "")
        ):
            continue
        relation_type = str(relation.get("relation_type") or "zotero_related")
        predicate = (
            relation_type
            if relation_type in {"cites", "cited_by"}
            else "zotero_related"
        )
        values = source.get("custody_relations")
        relations = dict(values) if isinstance(values, Mapping) else {}
        targets = relations.get(predicate, [])
        target_list = (
            list(targets) if isinstance(targets, list) else [targets] if targets else []
        )
        if target_id not in target_list:
            target_list.append(target_id)
        relations[predicate] = target_list
        source["custody_relations"] = relations
    return rows


def _project_navigation_onto_map(
    navigation: Mapping[str, Any],
    profiles: Sequence[Mapping[str, Any]],
    clusters: Sequence[dict[str, Any]],
    gaps: Sequence[dict[str, Any]],
    *,
    policy: Any = None,
) -> None:
    """Attach bounded graph projections after analytical identities are frozen."""

    assignments = [dict(row) for row in navigation.get("assignments", []) or []]
    tags_by_id = {
        str(row.get("subject_tag_id") or ""): row
        for row in navigation.get("subject_tags", []) or []
        if row.get("subject_tag_id")
    }
    assignments_by_source: dict[str, set[str]] = defaultdict(set)
    for assignment in assignments:
        if assignment.get("promotion_status", "promoted") != "promoted":
            continue
        assignments_by_source[str(assignment.get("source_id") or "")].add(
            str(assignment.get("subject_tag_id") or "")
        )
    family_by_source = {
        str(row.get("source_id") or ""): str(
            row.get("study_family_id") or row.get("source_id") or ""
        )
        for row in profiles
    }
    max_tags = int(_policy_value(policy, "max_visible_tags_per_cluster_or_gap", 6))
    max_neighborhoods = int(_policy_value(policy, "max_visible_neighborhoods", 8))
    facet_priority = {
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

    cluster_tags: dict[str, set[str]] = {}
    for cluster in clusters:
        core = set(str(value) for value in cluster.get("core_source_ids", []) or [])
        qualifying_counts: Counter[str] = Counter()
        for proposition in cluster.get("propositions", []) or []:
            sources = core & {
                str(value) for value in proposition.get("source_ids", []) or []
            }
            tag_sources: dict[str, set[str]] = defaultdict(set)
            for source_id in sources:
                for tag_id in assignments_by_source.get(source_id, set()):
                    tag_sources[tag_id].add(source_id)
            for tag_id, supporting_sources in tag_sources.items():
                families = {
                    family_by_source.get(source_id, source_id)
                    for source_id in supporting_sources
                }
                if len(families) >= 2:
                    qualifying_counts[tag_id] = max(
                        qualifying_counts[tag_id], len(families)
                    )
        ranked_tag_ids = sorted(
            qualifying_counts,
            key=lambda tag_id: (
                -qualifying_counts[tag_id],
                facet_priority.get(
                    str(tags_by_id.get(tag_id, {}).get("facet_type") or ""), 99
                ),
                str(tags_by_id.get(tag_id, {}).get("canonical_tag") or tag_id),
            ),
        )[:max_tags]
        visible_neighborhoods = rank_topic_neighborhoods(
            navigation.get("topic_neighborhoods", []) or [],
            list(core),
            proposition_tag_ids=ranked_tag_ids,
            max_visible=max_neighborhoods,
        )
        cluster["subject_tag_ids"] = ranked_tag_ids
        cluster["subject_tags"] = [
            str(tags_by_id[tag_id].get("canonical_tag") or "")
            for tag_id in ranked_tag_ids
            if tag_id in tags_by_id
        ]
        cluster["topic_neighborhood_ids"] = [
            str(row.get("topic_neighborhood_id") or "") for row in visible_neighborhoods
        ]
        cluster["topic_neighborhoods"] = [
            {
                "topic_neighborhood_id": str(row.get("topic_neighborhood_id") or ""),
                "label": str(row.get("label") or ""),
                "facet_type": str(row.get("facet_type") or ""),
                "canonical_tag": str(
                    tags_by_id.get(str(row.get("canonical_tag_id") or ""), {}).get(
                        "canonical_tag"
                    )
                    or ""
                ),
                "cluster_member_count": int(row.get("cluster_member_count", 0) or 0),
            }
            for row in visible_neighborhoods
        ]
        cluster["navigation_projection_hash"] = _stable_hash(
            {
                "cluster_id": cluster.get("cluster_id"),
                "subject_tag_ids": ranked_tag_ids,
                "topic_neighborhood_ids": cluster["topic_neighborhood_ids"],
            }
        )
        cluster_tags[str(cluster.get("cluster_id") or "")] = set(ranked_tag_ids)

    cluster_ids_by_neighborhood: dict[str, set[str]] = defaultdict(set)
    for cluster in clusters:
        cluster_id = str(cluster.get("cluster_id") or "")
        if not cluster_id:
            continue
        for neighborhood_id in cluster.get("topic_neighborhood_ids", []) or []:
            if neighborhood_id:
                cluster_ids_by_neighborhood[str(neighborhood_id)].add(cluster_id)
    for summary in navigation.get("human_neighborhood_summaries", []) or []:
        if not isinstance(summary, dict):
            continue
        neighborhood_id = str(summary.get("neighborhood_id") or "")
        summary["related_cluster_ids"] = sorted(
            cluster_ids_by_neighborhood.get(neighborhood_id, set())
        )

    propositions_by_id = {
        str(proposition.get("proposition_id") or ""): proposition
        for cluster in clusters
        for proposition in cluster.get("propositions", []) or []
        if proposition.get("proposition_id")
    }
    for gap in gaps:
        eligible_tag_ids: set[str] = set()
        for cluster_id in gap.get("related_cluster_ids", []) or []:
            eligible_tag_ids.update(cluster_tags.get(str(cluster_id), set()))
        for proposition_id in gap.get("proposition_ids", []) or []:
            proposition = propositions_by_id.get(str(proposition_id), {})
            source_ids = [
                str(value) for value in proposition.get("source_ids", []) or []
            ]
            if not source_ids:
                continue
            shared = (
                set.intersection(
                    *(
                        assignments_by_source.get(source_id, set())
                        for source_id in source_ids
                    )
                )
                if source_ids
                else set()
            )
            eligible_tag_ids.update(shared)
        ranked_gap_tags = sorted(
            (tag_id for tag_id in eligible_tag_ids if tag_id in tags_by_id),
            key=lambda tag_id: (
                facet_priority.get(str(tags_by_id[tag_id].get("facet_type") or ""), 99),
                str(tags_by_id[tag_id].get("canonical_tag") or tag_id),
            ),
        )[:max_tags]
        gap["subject_tag_ids"] = ranked_gap_tags
        gap["subject_tags"] = [
            str(tags_by_id[tag_id].get("canonical_tag") or "")
            for tag_id in ranked_gap_tags
        ]
        gap["navigation_projection_hash"] = _stable_hash(
            {"gap_id": gap.get("gap_id"), "subject_tag_ids": ranked_gap_tags}
        )


def _project_cross_cluster_relationships(
    clusters: Sequence[Mapping[str, Any]],
    cluster_syntheses: Mapping[str, dict[str, Any]],
    profiles: Sequence[Mapping[str, Any]],
    typed_relations: Sequence[Mapping[str, Any]],
    *,
    max_per_cluster: int = 3,
) -> None:
    """Project reciprocal, source-backed cluster bridges without analytical force.

    Topic neighborhoods and inferred similarity are intentionally excluded.
    These links help a researcher navigate the map; they never change cluster
    admission, debate classification, or gap promotion.
    """

    cluster_by_id = {
        str(cluster.get("cluster_id") or ""): cluster
        for cluster in clusters
        if cluster.get("cluster_id")
    }
    profile_by_source = {
        str(profile.get("source_id") or ""): profile
        for profile in profiles
        if profile.get("source_id")
    }
    role_by_cluster = {
        cluster_id: {
            str(row.get("source_id") or ""): str(row.get("role") or "context")
            for row in cluster.get("source_roles", []) or []
            if isinstance(row, Mapping) and row.get("source_id")
        }
        for cluster_id, cluster in cluster_by_id.items()
    }

    def contribution(
        cluster_id: str, source_id: str
    ) -> tuple[Mapping[str, Any], list[dict[str, Any]]] | None:
        synthesis = cluster_syntheses.get(cluster_id, {})
        for row in synthesis.get("source_contributions", []) or []:
            if not isinstance(row, Mapping) or str(row.get("source_id") or "") != source_id:
                continue
            evidence = [
                dict(reference)
                for reference in row.get("evidence", []) or []
                if isinstance(reference, Mapping)
                and str(reference.get("source_id") or "") == source_id
                and _human_locator_text(reference.get("locator") or "")
            ]
            if evidence:
                return row, evidence
        return None

    def label(cluster_id: str) -> str:
        cluster = cluster_by_id[cluster_id]
        return _human_projection_text(
            cluster.get("display_label") or cluster.get("label") or cluster_id
        )

    existing_targets: dict[str, set[str]] = defaultdict(set)
    # Validated model relationships are already evidence-backed. Mirror them
    # so both Obsidian notes expose the same edge.
    for cluster_id, synthesis in cluster_syntheses.items():
        if cluster_id not in cluster_by_id:
            continue
        for raw in list(synthesis.get("related_clusters", []) or []):
            if not isinstance(raw, Mapping):
                continue
            target_id = str(
                raw.get("target_cluster_id")
                or raw.get("related_cluster_id")
                or raw.get("cluster_id")
                or ""
            )
            if target_id not in cluster_by_id or target_id == cluster_id:
                continue
            existing_targets[cluster_id].add(target_id)
            reverse_synthesis = cluster_syntheses.get(target_id)
            if reverse_synthesis is None:
                continue
            reverse_targets = {
                str(
                    row.get("target_cluster_id")
                    or row.get("related_cluster_id")
                    or row.get("cluster_id")
                    or ""
                )
                for row in reverse_synthesis.get("related_clusters", []) or []
                if isinstance(row, Mapping)
            }
            if cluster_id in reverse_targets:
                existing_targets[target_id].add(cluster_id)
                continue
            reverse = dict(raw)
            reverse.update(
                {
                    "relationship_id": str(
                        raw.get("relationship_id")
                        or f"cluster-bridge-{_stable_hash(sorted((cluster_id, target_id)))[:14]}"
                    ),
                    "target_cluster_id": cluster_id,
                    "cluster_id": cluster_id,
                    "target_label": label(cluster_id),
                    "evidence": list(raw.get("target_evidence", []) or []),
                    "current_evidence": list(raw.get("target_evidence", []) or []),
                    "target_evidence": list(raw.get("current_evidence", []) or []),
                    "projection_origin": "reciprocal_validated_reasoner_edge",
                }
            )
            reverse_synthesis.setdefault("related_clusters", []).append(reverse)
            existing_targets[target_id].add(cluster_id)

    candidates: list[tuple[int, str, str, dict[str, Any], dict[str, Any]]] = []
    for left_id, right_id in combinations(sorted(cluster_by_id), 2):
        left_sources = {
            str(value) for value in cluster_by_id[left_id].get("source_ids", []) or []
        }
        right_sources = {
            str(value) for value in cluster_by_id[right_id].get("source_ids", []) or []
        }
        shared_sources = sorted(left_sources & right_sources)
        for source_id in shared_sources:
            left_role = role_by_cluster[left_id].get(source_id, "context")
            right_role = role_by_cluster[right_id].get(source_id, "context")
            if "core" not in {left_role, right_role}:
                continue
            left_contribution = contribution(left_id, source_id)
            right_contribution = contribution(right_id, source_id)
            if left_contribution is None or right_contribution is None:
                continue
            left_row, left_evidence = left_contribution
            right_row, right_evidence = right_contribution
            source_label = _source_attribution_label(
                profile_by_source.get(source_id, {}), source_id
            )
            left_finding = _map_verdict_excerpt(
                left_row.get("finding"), sentence_limit=1, character_limit=220
            )
            right_finding = _map_verdict_excerpt(
                right_row.get("finding"), sentence_limit=1, character_limit=220
            )
            if _canonical_phrase(left_finding) == _canonical_phrase(right_finding):
                relationship = (
                    f"Both clusters use {source_label} as background evidence. This shared source "
                    "connects the two questions but does not establish agreement between them."
                )
            else:
                relationship = (
                    f"{source_label} supplies located evidence to both literatures: in {label(left_id)}, "
                    f"it contributes {left_finding} In {label(right_id)}, it contributes {right_finding} "
                    "This is a source-level bridge, not evidence that the clusters agree."
                )
            pair_id = f"cluster-bridge-{_stable_hash([left_id, right_id, source_id])[:14]}"
            left_record = {
                "relationship_id": pair_id,
                "target_cluster_id": right_id,
                "cluster_id": right_id,
                "target_label": label(right_id),
                "relation_type": "shared_source_bridge",
                "relationship": relationship,
                "evidence": left_evidence,
                "current_evidence": left_evidence,
                "target_evidence": right_evidence,
                "projection_origin": "deterministic_shared_source_bridge",
            }
            right_record = {
                **left_record,
                "target_cluster_id": left_id,
                "cluster_id": left_id,
                "target_label": label(left_id),
                "evidence": right_evidence,
                "current_evidence": right_evidence,
                "target_evidence": left_evidence,
            }
            candidates.append((0, left_id, right_id, left_record, right_record))

    explicit_provenance = {
        "exact_source_relation",
        "inverse_exact_citation",
        "01_custody/source_relation_registry.csv",
    }
    for relation in typed_relations:
        if relation.get("inferred") is not False or str(
            relation.get("provenance") or ""
        ) not in explicit_provenance:
            continue
        source_id = str(relation.get("source_id") or "")
        target_source_id = str(relation.get("target_source_id") or "")
        for left_id, right_id in combinations(sorted(cluster_by_id), 2):
            orientations = (
                (source_id, target_source_id),
                (target_source_id, source_id),
            )
            matched = next(
                (
                    pair
                    for pair in orientations
                    if pair[0] in role_by_cluster[left_id]
                    and pair[1] in role_by_cluster[right_id]
                    and role_by_cluster[left_id][pair[0]] in {"core", "bridge"}
                    and role_by_cluster[right_id][pair[1]] in {"core", "bridge"}
                ),
                None,
            )
            if matched is None:
                continue
            left_source, right_source = matched
            left_contribution = contribution(left_id, left_source)
            right_contribution = contribution(right_id, right_source)
            if left_contribution is None or right_contribution is None:
                continue
            _, left_evidence = left_contribution
            _, right_evidence = right_contribution
            pair_id = f"cluster-bridge-{_stable_hash([left_id, right_id, relation.get('relation_id')])[:14]}"
            relationship = (
                f"An exact source relation connects {_source_attribution_label(profile_by_source.get(left_source, {}), left_source)} "
                f"in {label(left_id)} with {_source_attribution_label(profile_by_source.get(right_source, {}), right_source)} "
                f"in {label(right_id)}. This is a source-level bridge, not evidence that the clusters agree."
            )
            left_record = {
                "relationship_id": pair_id,
                "target_cluster_id": right_id,
                "cluster_id": right_id,
                "target_label": label(right_id),
                "relation_type": "explicit_source_link",
                "relationship": relationship,
                "evidence": left_evidence,
                "current_evidence": left_evidence,
                "target_evidence": right_evidence,
                "projection_origin": "deterministic_explicit_source_link",
            }
            right_record = {
                **left_record,
                "target_cluster_id": left_id,
                "cluster_id": left_id,
                "target_label": label(left_id),
                "evidence": right_evidence,
                "current_evidence": right_evidence,
                "target_evidence": left_evidence,
            }
            candidates.append((1, left_id, right_id, left_record, right_record))

    for _, left_id, right_id, left_record, right_record in sorted(
        candidates,
        key=lambda row: (row[0], row[1], row[2], row[3]["relationship_id"]),
    ):
        if right_id in existing_targets[left_id] or left_id in existing_targets[right_id]:
            continue
        if (
            len(existing_targets[left_id]) >= max_per_cluster
            or len(existing_targets[right_id]) >= max_per_cluster
        ):
            continue
        cluster_syntheses[left_id].setdefault("related_clusters", []).append(left_record)
        cluster_syntheses[right_id].setdefault("related_clusters", []).append(right_record)
        existing_targets[left_id].add(right_id)
        existing_targets[right_id].add(left_id)

    for synthesis in cluster_syntheses.values():
        synthesis["related_clusters"] = sorted(
            [
                dict(row)
                for row in synthesis.get("related_clusters", []) or []
                if isinstance(row, Mapping)
            ],
            key=lambda row: (
                str(row.get("target_cluster_id") or ""),
                str(row.get("relationship_id") or ""),
            ),
        )


def build_locator_audit(profiles: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for profile in profiles:
        for anchor in profile.get("claims", []) or []:
            locator = _as_mapping(anchor.get("source_locator")) or _source_locator(
                anchor.get("locator")
            )
            rows.append(
                {
                    "source_id": str(profile.get("source_id") or ""),
                    "evidence_anchor_id": str(
                        anchor.get("evidence_anchor_id") or anchor.get("claim_id") or ""
                    ),
                    "locator": str(anchor.get("locator") or ""),
                    "locator_kind": str(locator.get("kind") or "missing"),
                    "traceable": bool(locator.get("traceable")),
                    "strong_synthesis_support": bool(
                        locator.get("strong_synthesis_support")
                    ),
                    "rejection_reason": str(locator.get("rejection_reason") or ""),
                }
            )
    counts = Counter(str(row["locator_kind"]) for row in rows)
    return {
        "version": LOCATOR_AUDIT_VERSION,
        "anchor_count": len(rows),
        "strong_locator_count": sum(
            1 for row in rows if row["strong_synthesis_support"]
        ),
        "generated_note_heading_count": counts.get("generated_note_heading", 0),
        "locator_kind_counts": dict(sorted(counts.items())),
        "rows": sorted(
            rows, key=lambda row: (row["source_id"], row["evidence_anchor_id"])
        ),
    }


def build_coverage_register(
    profiles: Sequence[Mapping[str, Any]],
    *,
    source_set: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the strict public CoverageRegister for every frozen inventory row."""

    source_set = source_set or {}
    profile_by_source = {str(row.get("source_id") or ""): row for row in profiles}
    profile_by_note = {str(row.get("note_id") or ""): row for row in profiles}
    records: list[dict[str, Any]] = []
    inventory_rows = [
        dict(row)
        for row in source_set.get("rows", []) or []
        if isinstance(row, Mapping)
    ]
    if inventory_rows:
        for item in inventory_rows:
            source_id = str(item.get("source_id") or "")
            note_id = str(item.get("note_id") or "")
            profile = profile_by_source.get(source_id) or profile_by_note.get(note_id)
            terminal_status = str(item.get("terminal_status") or "pending")
            exclusion_reason = str(
                (profile or {}).get("exclusion_reason")
                or (
                    "source_processing_exhausted"
                    if terminal_status == "exhausted"
                    else ""
                )
            )
            records.append(
                {
                    "source_id": source_id,
                    "title": str(
                        (profile or {}).get("title")
                        or item.get("title")
                        or source_id
                        or note_id
                    ),
                    "zotero_key": str(item.get("zotero_item_key") or ""),
                    "terminal_state": terminal_status,
                    "exclusion_reason": exclusion_reason,
                    "attempted_route": [
                        str(value)
                        for value in item.get("attempted_route", []) or []
                        if str(value)
                    ],
                    "could_affect_existing_cluster": terminal_status
                    in {"limited_note", "exhausted"},
                }
            )
    else:
        for profile in profiles:
            terminal_status = (
                "validated_note" if profile.get("analytical") else "limited_note"
            )
            records.append(
                {
                    "source_id": str(profile.get("source_id") or ""),
                    "title": str(
                        profile.get("title") or profile.get("source_id") or ""
                    ),
                    "zotero_key": str(profile.get("zotero_item_key") or ""),
                    "terminal_state": terminal_status,
                    "exclusion_reason": str(profile.get("exclusion_reason") or ""),
                    "attempted_route": [],
                    "could_affect_existing_cluster": terminal_status == "limited_note",
                }
            )
    status_counts = Counter(
        str(row.get("terminal_state") or "pending") for row in records
    )
    counts = {
        "validated_note": status_counts.get("validated_note", 0),
        "limited_note": status_counts.get("limited_note", 0),
        "exhausted": status_counts.get("exhausted", 0),
        "partial": status_counts.get("partial", 0),
        "pending": status_counts.get("pending", 0),
    }
    status = (
        "partial"
        if counts["partial"] or counts["pending"]
        else "complete_with_exclusions"
        if counts["exhausted"]
        else "complete"
    )
    return {
        "source_set_id": str(source_set.get("source_set_id") or ""),
        "inventory_count": len(records),
        "counts": counts,
        "records": records,
        "status": status,
    }


def _coverage_repair_source_ids(
    clustered: Mapping[str, Any],
    profiles: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Return unclustered analytical sources with at least one usable anchor."""

    eligible_sources = {
        str(profile.get("source_id") or "")
        for profile in profiles
        if profile.get("analytical")
        and any(
            _anchor_is_synthesis_eligible(claim)
            for claim in profile.get("claims", []) or []
        )
    }
    return sorted(
        {
            source_id
            for row in clustered.get("unclustered_sources", []) or []
            if (source_id := str(row.get("source_id") or "")) in eligible_sources
        }
    )


def _coverage_signal_components(
    focus_source_ids: Sequence[str],
    profiles: Sequence[Mapping[str, Any]],
    relations: Sequence[Mapping[str, Any]],
    topic_neighborhoods: Sequence[Mapping[str, Any]],
) -> list[dict[str, list[str]]]:
    """Build semantic repair components without identifier-based micro-batches."""

    profile_by_source = {
        str(profile.get("source_id") or ""): profile
        for profile in profiles
        if profile.get("analytical") and profile.get("source_id")
    }
    eligible = set(profile_by_source)
    focus = {source_id for source_id in focus_source_ids if source_id in eligible}
    adjacency: dict[str, set[str]] = {source_id: set() for source_id in eligible}

    def connect(values: Sequence[Any]) -> None:
        source_ids = sorted({str(value) for value in values if str(value) in eligible})
        if len(source_ids) < 2:
            return
        for left in source_ids:
            adjacency[left].update(right for right in source_ids if right != left)

    for relation in relations:
        if isinstance(relation, Mapping):
            connect(relation.get("source_ids", []) or [])
    for neighborhood in topic_neighborhoods:
        if not isinstance(neighborhood, Mapping):
            continue
        if str(neighborhood.get("kind") or neighborhood.get("facet_type") or "") in {
            "period",
        }:
            continue
        connect(neighborhood.get("source_ids", []) or [])

    # Exact profile facets are candidate signals only. They connect the audit
    # packet; deterministic admission still decides whether a cluster exists.
    for field in ("concepts", "theories", "mechanisms", "outcomes", "cases", "methods"):
        sources_by_value: dict[str, list[str]] = defaultdict(list)
        for source_id, profile in profile_by_source.items():
            for value in profile.get(field, []) or []:
                normalized = _canonical_phrase(value)
                if normalized:
                    sources_by_value[normalized].append(source_id)
        for source_ids in sources_by_value.values():
            connect(source_ids)

    components: list[dict[str, list[str]]] = []
    remaining = set(focus)
    while remaining:
        seed = min(remaining)
        stack = [seed]
        connected: set[str] = set()
        while stack:
            current = stack.pop()
            if current in connected:
                continue
            connected.add(current)
            stack.extend(adjacency.get(current, set()) - connected)
        focus_component = connected & focus
        remaining -= focus_component
        if not focus_component:
            continue
        context_sources = set(focus_component)
        for source_id in focus_component:
            context_sources.update(adjacency.get(source_id, set()))
        components.append(
            {
                "focus_source_ids": sorted(focus_component),
                "source_ids": sorted(context_sources),
            }
        )
    return sorted(
        components,
        key=lambda row: (
            -len(row["focus_source_ids"]),
            _stable_hash(row["focus_source_ids"]),
        ),
    )


def _coverage_audit_plan(
    clustered: Mapping[str, Any],
    profiles: Sequence[Mapping[str, Any]],
    relations: Sequence[Mapping[str, Any]],
    topic_neighborhoods: Sequence[Mapping[str, Any]],
    *,
    reasoner: Any,
    request: Any,
) -> list[dict[str, Any]]:
    """Choose one coarse DeepSeek audit or semantic whole-profile components."""

    focus_source_ids = _coverage_repair_source_ids(clustered, profiles)
    if not focus_source_ids:
        return []
    analytical_source_ids = sorted(
        str(profile.get("source_id") or "")
        for profile in profiles
        if profile.get("analytical") and profile.get("source_id")
    )
    provider = str(
        getattr(reasoner, "name", "")
        or _as_mapping(request).get("provider")
        or ""
    ).casefold()
    model = str(
        getattr(reasoner, "model", "")
        or _as_mapping(request).get("model")
        or ""
    ).casefold()
    context_window = int(getattr(reasoner, "context_window_tokens", 0) or 0)
    policy = _as_mapping(_as_mapping(request).get("literature_policy"))
    context_fraction = float(policy.get("deepseek_packet_context_fraction", 0.8) or 0.8)
    estimated_tokens = max(
        1,
        len(
            json.dumps(
                {
                    "profiles": profiles,
                    "relations": relations,
                    "topic_neighborhoods": topic_neighborhoods,
                    "clusters": clustered.get("clusters", []),
                    "unclustered": clustered.get("unclustered_sources", []),
                },
                sort_keys=True,
                default=str,
            )
        )
        // 4,
    )
    if (
        "deepseek" in {provider, model}
        or "deepseek" in provider
        or "deepseek" in model
    ) and context_window > 0 and estimated_tokens <= int(context_window * context_fraction):
        return [
            {
                "mode": "collection",
                "key": "collection--coverage-audit",
                "focus_source_ids": focus_source_ids,
                "source_ids": analytical_source_ids,
            }
        ]

    components = _coverage_signal_components(
        focus_source_ids, profiles, relations, topic_neighborhoods
    )
    return [
        {
            "mode": "semantic_component",
            "key": "collection--coverage-component-"
            + _stable_hash(
                {
                    "focus_source_ids": component["focus_source_ids"],
                    "source_ids": component["source_ids"],
                }
            )[:12],
            **component,
        }
        for component in components
    ]


def _cluster_proposals_from_responses(
    responses: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Merge proposal packets while preferring the latest version of an identity."""

    merged: dict[str, dict[str, Any]] = {}
    for response in responses:
        if not isinstance(response, Mapping):
            continue
        for raw in response.get("clusters", []) or []:
            if not isinstance(raw, Mapping):
                continue
            proposal = dict(raw)
            identity = _canonical_phrase(
                proposal.get("semantic_identity")
                or proposal.get("label")
                or proposal.get("shared_question")
                or ""
            )
            key = identity or _stable_hash(proposal)
            merged[key] = proposal
    return list(merged.values())


def _cluster_synthesis_response_budget(
    cluster: Mapping[str, Any],
) -> dict[str, int]:
    """Constrain structurally large cluster responses without shrinking evidence coverage."""

    core_source_ids = {
        str(row.get("source_id") or "")
        for row in cluster.get("source_roles", []) or []
        if isinstance(row, Mapping) and str(row.get("role") or "") == "core"
    } or {str(value) for value in cluster.get("core_source_ids", []) or []}
    member_source_ids = {
        str(value) for value in cluster.get("source_ids", []) or [] if str(value)
    } or core_source_ids
    if len(member_source_ids) < 5:
        return {}
    return {
        "max_output_tokens": 4_500,
        "max_evidence_threads": 3,
        "source_contributions_per_core": 1,
        "max_central_findings": 3,
        "max_items_per_optional_section": 2,
        "max_gap_hypotheses": 2,
    }


def build_literature_report(
    profiles: Sequence[Any],
    *,
    previous_registry: Mapping[str, Any] | None = None,
    policy: Any = None,
    question: str | None = None,
    reasoner: Any = None,
    request: Any = None,
    stage_callback: Any = None,
    reasoner_call: Any = None,
    source_notes: Sequence[Mapping[str, Any]] = (),
    navigation_policy: Any = None,
    source_set: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Pure end-to-end mapper over already-built evidence profiles."""
    normalized = normalize_evidence_profiles(profiles)
    note_anchor_mismatch_count = _reconcile_profile_anchors_with_atomic_notes(
        normalized, source_notes
    )
    independence = build_independence_records(normalized)
    locator_audit = build_locator_audit(normalized)
    coverage_register = build_coverage_register(normalized, source_set=source_set)
    _notify_stage(stage_callback, "evidence_anchors")
    _notify_stage(stage_callback, "relation_mapping")
    relations = map_profile_relations(normalized)
    _notify_stage(stage_callback, "topic_neighborhoods")
    topic_neighborhoods = map_topic_neighborhoods(normalized, relations)
    _notify_stage(stage_callback, "proposition_mapping")
    propositions = build_literature_propositions(normalized)
    _notify_stage(stage_callback, "clustering")
    analytical_families = {
        str(row.get("study_family_id") or row.get("source_id") or "")
        for row in normalized
        if row.get("analytical")
    }
    proposal_response = (
        _reasoner_stage(
            reasoner,
            reasoner_call,
            stage="cluster_proposal",
            key="collection",
            method_name="propose_clusters",
            profiles=normalized,
            request=request,
            context={
                "propositions": propositions,
                "relations": relations,
                "topic_neighborhoods": topic_neighborhoods,
                "study_lineages": independence["study_lineages"],
                "evidence_base_groups": independence["evidence_base_groups"],
                "independence_assessments": independence["independence_assessments"],
            },
        )
        if len(analytical_families) >= 2
        else {}
    )
    clustered = map_overlapping_clusters(
        normalized,
        relations,
        policy=policy,
        proposals=list(proposal_response.get("clusters", []) or []),
        propositions=propositions,
        topic_neighborhoods=topic_neighborhoods,
    )
    proposal_responses: list[Mapping[str, Any]] = [proposal_response]
    combined_proposals = _cluster_proposals_from_responses(proposal_responses)
    completed_responses = getattr(reasoner_call, "completed_responses", None)
    coverage_plan = (
        _coverage_audit_plan(
            clustered,
            normalized,
            relations,
            topic_neighborhoods,
            reasoner=reasoner,
            request=request,
        )
        if len(analytical_families) >= 2 and reasoner_call is not None
        else []
    )
    for audit_index, audit in enumerate(coverage_plan, start=1):
        audit_source_ids = list(audit.get("source_ids", []) or [])
        focus_source_ids = list(audit.get("focus_source_ids", []) or [])
        if len(audit_source_ids) < 2 or not focus_source_ids:
            continue
        audit_key = str(audit.get("key") or "")
        repair_response = _reasoner_stage(
            reasoner,
            reasoner_call,
            stage="cluster_proposal",
            key=audit_key,
            method_name="propose_clusters",
            profiles=normalized,
            request=request,
            context={
                "propositions": propositions,
                "relations": relations,
                "topic_neighborhoods": topic_neighborhoods,
                # Keep the legacy field for custom reasoners while the built-in
                # reader uses the broader component packet.
                "coverage_repair_source_ids": focus_source_ids,
                "coverage_focus_source_ids": focus_source_ids,
                "coverage_component_source_ids": audit_source_ids,
                "coverage_audit_mode": str(audit.get("mode") or "semantic_component"),
                "coverage_component_signature": audit_key,
                "coverage_repair_attempt": audit_index,
                "current_clusters": list(clustered.get("clusters", []) or []),
                "current_unclustered_sources": list(
                    clustered.get("unclustered_sources", []) or []
                ),
                "prior_proposal_identities": [
                    str(row.get("semantic_identity") or row.get("label") or "")
                    for row in combined_proposals
                    if isinstance(row, Mapping)
                ],
                "prior_proposals": combined_proposals,
                "study_lineages": independence["study_lineages"],
                "evidence_base_groups": independence["evidence_base_groups"],
                "independence_assessments": independence["independence_assessments"],
            },
        )
        preserved_repair_responses: list[Mapping[str, Any]] = []
        if callable(completed_responses):
            preserved_repair_responses = list(
                completed_responses("cluster_proposal", audit_key) or []
            )
        proposal_responses.extend([*preserved_repair_responses, repair_response])
        combined_proposals = _cluster_proposals_from_responses(proposal_responses)
        clustered = map_overlapping_clusters(
            normalized,
            relations,
            policy=policy,
            proposals=combined_proposals,
            propositions=propositions,
            topic_neighborhoods=topic_neighborhoods,
        )
    _notify_stage(stage_callback, "evidence_matrices")
    admission_matrices = build_evidence_matrices(normalized, clustered["clusters"])
    admitted_cluster_ids = {
        str(row["cluster_id"])
        for row in admission_matrices
        if row.get("admission_passed")
    }
    rejected_matrix_clusters = [
        {
            "proposal_id": str(cluster.get("proposal_id") or ""),
            "semantic_identity": str(cluster.get("semantic_identity") or ""),
            "source_ids": list(cluster.get("source_ids", []) or []),
            "action": "reject",
            "reason": "debate_family_matrix_failed_locator_or_connectivity_validation",
        }
        for cluster in clustered["clusters"]
        if str(cluster["cluster_id"]) not in admitted_cluster_ids
    ]
    if rejected_matrix_clusters:
        clustered["rejected_proposals"] = [
            *clustered["rejected_proposals"],
            *rejected_matrix_clusters,
        ]
    registry = reconcile_cluster_registry(
        [
            cluster
            for cluster in clustered["clusters"]
            if str(cluster["cluster_id"]) in admitted_cluster_ids
        ],
        previous_registry,
    )
    _apply_researcher_display_safeguards(registry["clusters"], normalized)
    mapped_propositions = sorted(
        {
            str(proposition["proposition_id"]): dict(proposition)
            for cluster in registry["clusters"]
            for proposition in cluster.get("propositions", []) or []
            if str(proposition.get("proposition_id") or "")
        }.values(),
        key=lambda row: str(row["proposition_id"]),
    )
    _notify_stage(stage_callback, "support_validation")
    matrices = build_evidence_matrices(normalized, registry["clusters"])
    deterministic_debates = build_debate_registry(
        normalized, registry["clusters"], policy=policy
    )
    matrix_by_cluster = {str(row["cluster_id"]): row for row in matrices}
    deterministic_debate_by_cluster = {
        str(row["cluster_id"]): row for row in deterministic_debates["assessments"]
    }
    source_note_by_source = {
        str(row.get("source_id") or ""): row
        for row in source_notes
        if isinstance(row, Mapping) and row.get("source_id")
    }
    cluster_context_catalog = [
        {
            "cluster_id": str(row.get("cluster_id") or ""),
            "label": _human_projection_text(
                row.get("display_label") or row.get("label") or ""
            ),
            "question": _human_projection_text(
                row.get("display_question") or row.get("shared_question") or ""
            ),
            "bounded_object": _human_projection_text(row.get("bounded_object") or ""),
            "core_source_ids": list(row.get("core_source_ids", []) or []),
            "propositions": [
                {
                    "proposition_id": str(proposition.get("proposition_id") or ""),
                    "statement": _human_projection_text(
                        proposition.get("statement") or ""
                    ),
                }
                for proposition in row.get("propositions", []) or []
                if isinstance(proposition, Mapping)
            ][:4],
            "evidence": [
                dict(reference)
                for reference in row.get("proposal_supporting_evidence", []) or []
                if isinstance(reference, Mapping)
            ][:6],
        }
        for row in registry["clusters"]
    ]
    cluster_syntheses: dict[str, dict[str, Any]] = {}
    synthesis_order = sorted(
        registry["clusters"],
        key=lambda row: (
            str(row.get("formation_route") or "") != "reasoner_proposal",
            str(row.get("status") or "") == "emerging_cluster",
            -int(row.get("source_count", 0) or 0),
            str(row.get("cluster_id") or ""),
        ),
    )
    for cluster in synthesis_order:
        cluster_id = str(cluster["cluster_id"])
        response_budget = _cluster_synthesis_response_budget(cluster)
        _notify_stage(stage_callback, "cluster_synthesis", active_cluster=cluster_id)
        member_profiles = [
            row
            for row in normalized
            if str(row.get("source_id") or "")
            in set(cluster.get("source_ids", []) or [])
        ]
        atomic_notes = [
            {
                "source_id": source_id,
                "title": str(source_note_by_source[source_id].get("title") or ""),
                "note_path": str(
                    source_note_by_source[source_id].get("note_path") or ""
                ),
                "atomic_note": str(source_note_by_source[source_id].get("body") or ""),
            }
            for source_id in cluster.get("source_ids", []) or []
            if source_id in source_note_by_source
            and source_note_by_source[source_id].get("body")
        ]
        synthesis_response = _reasoner_stage(
            reasoner,
            reasoner_call,
            stage="cluster_synthesis",
            key=cluster_id,
            method_name="synthesize_cluster",
            profiles=member_profiles,
            request=request,
            context={
                "cluster": cluster,
                "evidence_matrix": matrix_by_cluster.get(cluster_id, {}),
                "deterministic_debate": deterministic_debate_by_cluster.get(
                    cluster_id, {}
                ),
                "all_cluster_ids": [row["cluster_id"] for row in registry["clusters"]],
                "all_clusters": cluster_context_catalog,
                "study_lineages": [
                    row
                    for row in independence["study_lineages"]
                    if set(str(value) for value in row.get("source_ids", []) or [])
                    & set(cluster.get("source_ids", []) or [])
                ],
                "evidence_base_groups": [
                    row
                    for row in independence["evidence_base_groups"]
                    if set(row.get("source_ids", []) or [])
                    & set(cluster.get("source_ids", []) or [])
                ],
                "required_source_contributions": _fallback_source_contributions(
                    cluster, normalized
                ),
                "atomic_notes": atomic_notes,
                **({"response_budget": response_budget} if response_budget else {}),
            },
        )
        validated_synthesis = validate_cluster_synthesis(
            synthesis_response,
            cluster,
            normalized,
            deterministic_debate=deterministic_debate_by_cluster.get(cluster_id, {}),
            all_clusters=registry["clusters"],
        )
        if validated_synthesis.get("status") == "partial" and reasoner_call is not None:
            repair_response = _reasoner_stage(
                reasoner,
                reasoner_call,
                stage="cluster_synthesis",
                key=f"{cluster_id}--repair",
                method_name="synthesize_cluster",
                profiles=member_profiles,
                request=request,
                context={
                    "cluster": cluster,
                    "evidence_matrix": matrix_by_cluster.get(cluster_id, {}),
                    "deterministic_debate": deterministic_debate_by_cluster.get(
                        cluster_id, {}
                    ),
                    "all_cluster_ids": [
                        row["cluster_id"] for row in registry["clusters"]
                    ],
                    "all_clusters": cluster_context_catalog,
                    "repair_requirements": list(
                        validated_synthesis.get("quality_errors", []) or []
                    ),
                    "previous_response": synthesis_response,
                    "required_source_contributions": _fallback_source_contributions(
                        cluster, normalized
                    ),
                    "atomic_notes": atomic_notes,
                    **({"response_budget": response_budget} if response_budget else {}),
                },
            )
            repaired_synthesis = validate_cluster_synthesis(
                repair_response,
                cluster,
                normalized,
                deterministic_debate=deterministic_debate_by_cluster.get(
                    cluster_id, {}
                ),
                all_clusters=registry["clusters"],
            )
            repaired_synthesis["repair_attempted"] = True
            if repaired_synthesis.get("status") == "reasoned" or len(
                repaired_synthesis.get("quality_errors", []) or []
            ) < len(validated_synthesis.get("quality_errors", []) or []):
                validated_synthesis = repaired_synthesis
            else:
                validated_synthesis["repair_attempted"] = True
        cluster_syntheses[cluster_id] = validated_synthesis
    quantitative_comparisons = _quantitative_comparison_records(cluster_syntheses)
    _notify_stage(stage_callback, "debate_mapping", active_cluster="")
    debates = apply_cluster_syntheses_to_debates(
        deterministic_debates,
        cluster_syntheses,
        policy=policy,
    )
    _notify_stage(stage_callback, "gap_detection")
    generated_candidates = generate_gap_candidates(
        normalized,
        registry["clusters"],
        debates,
        matrices,
        cluster_syntheses=cluster_syntheses,
    )
    specificity_rejections = [
        {
            **row,
            "status": "underspecified_gap",
            "promoted": False,
            "automation_status": "rejected",
        }
        for row in generated_candidates
        if row.get("specificity_errors")
    ]
    candidates = [
        row for row in generated_candidates if not row.get("specificity_errors")
    ]
    _notify_stage(stage_callback, "internal_falsification")
    validated, search_log = search_and_validate_gaps(
        candidates, normalized, policy=policy
    )
    deterministic_rejections = [
        row
        for row in validated
        if row.get("status") in {"answered_within_collection", "underspecified_gap"}
    ]
    visible_validated = [
        row
        for row in validated
        if row.get("status") not in {"answered_within_collection", "underspecified_gap"}
    ]
    _notify_stage(stage_callback, "resolution_paths")
    adjudication_response = (
        _reasoner_stage(
            reasoner,
            reasoner_call,
            stage="gap_adjudication",
            key="collection",
            method_name="detect_gaps",
            profiles=normalized,
            request=request,
            context={
                "clusters": registry["clusters"],
                "cluster_syntheses": cluster_syntheses,
                "candidates": visible_validated,
                "internal_search_log": search_log,
            },
        )
        if visible_validated
        else {}
    )
    adjudicated_gaps, reasoner_rejections = _apply_gap_adjudication(
        visible_validated,
        adjudication_response,
        normalized,
        cluster_syntheses,
        policy=policy,
    )
    gap_merge_ledger = sorted(
        [
            dict(event)
            for gap in adjudicated_gaps
            for event in gap.pop("merge_events", []) or []
        ],
        key=lambda row: (row["canonical_gap_id"], row["merged_gap_id"]),
    )
    gaps = rank_gap_registry(adjudicated_gaps)
    rejected_gaps = sorted(
        [*specificity_rejections, *deterministic_rejections, *reasoner_rejections],
        key=lambda row: str(row.get("gap_id") or ""),
    )
    # Materialize the reciprocal graph relationship only after final
    # collection-wide adjudication. It is a projection and does not alter the
    # cluster membership revision hash.
    valid_cluster_ids = {str(row["cluster_id"]) for row in registry["clusters"]}
    for gap in gaps:
        gap["related_cluster_ids"] = sorted(
            {
                str(value)
                for value in gap.get("related_cluster_ids", []) or []
                if str(value) in valid_cluster_ids
            }
        )
    gap_ids_by_cluster: dict[str, list[str]] = defaultdict(list)
    for gap in gaps:
        for cluster_id in gap.get("related_cluster_ids", []) or []:
            cluster_id = str(cluster_id)
            if cluster_id in valid_cluster_ids:
                gap_ids_by_cluster[cluster_id].append(str(gap["gap_id"]))
    for cluster in registry["clusters"]:
        cluster["related_gap_ids"] = sorted(
            set(gap_ids_by_cluster.get(str(cluster["cluster_id"]), []))
        )
    navigation_profiles = _navigation_profile_rows(
        [_as_mapping(profile) for profile in profiles], source_notes
    )
    if bool(_policy_value(navigation_policy, "subject_tags_enabled", True)):
        navigation = build_navigation_graph(
            navigation_profiles,
            propositions=mapped_propositions,
            max_candidates_per_source=int(
                _policy_value(navigation_policy, "max_candidate_tags_per_source", 24)
            ),
            max_visible_tags_per_source=int(
                _policy_value(navigation_policy, "max_visible_tags_per_source", 8)
            ),
            max_inferred_links_per_source=int(
                _policy_value(navigation_policy, "max_inferred_related_note_links", 8)
            ),
            minimum_neighborhood_sources=int(
                _policy_value(navigation_policy, "min_sources_per_neighborhood", 2)
            ),
        )
    else:
        typed_relations = build_typed_source_relations(
            navigation_profiles,
            propositions=mapped_propositions,
            max_inferred_links_per_source=int(
                _policy_value(navigation_policy, "max_inferred_related_note_links", 8)
            ),
        )
        navigation = {
            "tag_reconciliation_version": "1",
            "navigation_relation_version": "1",
            "neighborhood_promotion_version": "1",
            "subject_tags": [],
            "assignments": [],
            "candidates": [],
            "rejected_candidates": [],
            "candidate_count": 0,
            "promoted_subject_tag_count": 0,
            "rejected_generic_tag_count": 0,
            "unconfirmed_zotero_tag_count": 0,
            "topic_neighborhoods": [],
            "singleton_facets": [],
            "promoted_neighborhood_count": 0,
            "singleton_facet_count": 0,
            "typed_relations": typed_relations,
            "typed_relation_counts": dict(
                Counter(str(row.get("relation_type") or "") for row in typed_relations)
            ),
            "graph_projection_hash": _stable_hash(
                {"typed_relations": typed_relations, "subject_tags_enabled": False}
            ),
        }
    _project_navigation_onto_map(
        navigation,
        navigation_profiles,
        registry["clusters"],
        gaps,
        policy=navigation_policy,
    )
    _project_cross_cluster_relationships(
        registry["clusters"],
        cluster_syntheses,
        normalized,
        navigation.get("typed_relations", []) or [],
    )
    _notify_stage(stage_callback, "projection")
    gap_memory = [
        {
            "gap_id": gap["gap_id"],
            "rule": gap["rule"],
            "status": gap["status"],
            "revision_hash": _stable_hash(
                {
                    "gap_id": gap["gap_id"],
                    "supporting_evidence": gap.get("supporting_evidence", []),
                    "countervailing_evidence": gap.get("countervailing_evidence", []),
                    "status": gap["status"],
                    "value_assessment": gap.get("value_assessment", {}),
                    "resolution_path": gap.get("resolution_path", {}),
                    "proposition_ids": gap.get("proposition_ids", []),
                    "originating_cluster_revisions": gap.get(
                        "originating_cluster_revisions", []
                    ),
                    "missing_cell": gap.get("missing_cell", {}),
                    "anchors": gap.get("anchors", []),
                    "merged_from_gap_ids": gap.get("merged_from_gap_ids", []),
                    "reframed_from_gap_id": gap.get("reframed_from_gap_id", ""),
                    "priority_tier": gap.get("priority_tier", ""),
                    "structured_signature": gap.get("structured_signature", ""),
                    "strict_adjudication": gap.get("strict_adjudication", {}),
                }
            ),
        }
        for gap in gaps
    ]
    strict_claim_adjudications = [
        adjudication
        for assessment in debates.get("assessments", []) or []
        for adjudication in assessment.get("strict_adjudications", []) or []
        if isinstance(adjudication, Mapping)
    ]
    strict_gap_adjudications = [
        adjudication
        for gap in gaps
        if isinstance((adjudication := gap.get("strict_adjudication")), Mapping)
        and adjudication
    ]

    def strict_adjudication_count(kind: str, decision: str) -> int:
        rows = (
            strict_gap_adjudications
            if kind == "strong_gap"
            else strict_claim_adjudications
        )
        return sum(
            1
            for row in rows
            if row.get("kind") == kind and row.get("decision") == decision
        )

    analytical_source_ids = {
        str(row.get("source_id") or "") for row in normalized if row.get("analytical")
    }
    unclustered_analytical_count = sum(
        1
        for row in clustered["unclustered_sources"]
        if str(row.get("source_id") or "") in analytical_source_ids
    )
    manifest = {
        "mapper_version": "0.10.0",
        "algorithm_version": LITERATURE_ALGORITHM_VERSION,
        "profile_count": len(normalized),
        "analytical_profile_count": sum(1 for row in normalized if row["analytical"]),
        "limited_profile_count": sum(1 for row in normalized if row["limited"]),
        "note_anchor_mismatch_count": note_anchor_mismatch_count,
        "relation_count": len(navigation["typed_relations"]),
        "typed_relation_counts": dict(navigation.get("typed_relation_counts", {})),
        "subject_tag_count": int(navigation.get("promoted_subject_tag_count", 0) or 0),
        "subject_tag_assignment_count": len(navigation.get("assignments", []) or []),
        "topic_neighborhood_count": len(navigation["topic_neighborhoods"]),
        "singleton_facet_count": int(navigation.get("singleton_facet_count", 0) or 0),
        "graph_projection_hash": str(navigation.get("graph_projection_hash") or ""),
        "proposition_count": len(mapped_propositions),
        "cluster_count": len(registry["clusters"]),
        "source_backed_cluster_count": sum(
            1
            for row in registry["clusters"]
            if row["qualification_status"] == "source_backed_cluster"
        ),
        "emerging_cluster_count": sum(
            1
            for row in registry["clusters"]
            if row["qualification_status"] == "emerging_cluster"
        ),
        "evidence_concentrated_cluster_count": sum(
            1
            for row in registry["clusters"]
            if row["qualification_status"] == "evidence_concentrated_cluster"
        ),
        "promoted_cluster_count": sum(
            1 for row in registry["clusters"] if row["promoted"]
        ),
        "cluster_candidate_count": sum(
            1 for row in registry["clusters"] if not row["promoted"]
        ),
        "unclustered_source_count": unclustered_analytical_count,
        "debate_count": debates["debate_count"],
        "debate_candidate_count": debates["debate_candidate_count"],
        "gap_count": len(gaps),
        "promoted_gap_count": sum(1 for row in gaps if row["promoted"]),
        "gap_lead_count": sum(
            1 for row in gaps if row["status"] == "collection_gap_lead"
        ),
        "strict_consensus_established_count": strict_adjudication_count(
            "consensus", "established"
        ),
        "strict_consensus_not_established_count": strict_adjudication_count(
            "consensus", "not_established"
        ),
        "strict_contradiction_established_count": strict_adjudication_count(
            "contradiction", "established"
        ),
        "strict_contradiction_not_established_count": strict_adjudication_count(
            "contradiction", "not_established"
        ),
        "strong_gap_established_count": strict_adjudication_count(
            "strong_gap", "established"
        ),
        "strong_gap_not_established_count": strict_adjudication_count(
            "strong_gap", "not_established"
        ),
        "synthesized_cluster_count": sum(
            1 for row in cluster_syntheses.values() if row.get("status") == "reasoned"
        ),
        "partial_cluster_synthesis_count": sum(
            1 for row in cluster_syntheses.values() if row.get("status") == "partial"
        ),
        "rejected_underspecified_gap_count": sum(
            1 for row in rejected_gaps if row.get("status") == "underspecified_gap"
        ),
        "rejected_gap_quality_count": sum(
            1 for row in rejected_gaps if row.get("quality_rejection_reasons")
        ),
        "merged_gap_count": len(gap_merge_ledger),
        "study_lineage_count": len(independence["study_lineages"]),
        "evidence_base_group_count": len(independence["evidence_base_groups"]),
        "independence_assessment_count": len(independence["independence_assessments"]),
        "cluster_source_contribution_count": sum(
            len(row.get("source_contributions", []) or [])
            for row in cluster_syntheses.values()
        ),
        "quantitative_comparison_count": len(quantitative_comparisons),
        "rejected_quantitative_comparison_count": sum(
            1 for row in quantitative_comparisons if row.get("status") == "rejected"
        ),
        "strong_locator_count": int(locator_audit.get("strong_locator_count", 0) or 0),
        "rejected_generated_locator_count": int(
            locator_audit.get("generated_note_heading_count", 0) or 0
        ),
        "coverage_inventory_count": int(
            coverage_register.get("inventory_count", 0) or 0
        ),
        "coverage_exhausted_count": int(
            _as_mapping(coverage_register.get("counts")).get("exhausted", 0) or 0
        ),
        "coverage_accounting_valid": sum(
            int(_as_mapping(coverage_register.get("counts")).get(key, 0) or 0)
            for key in (
                "validated_note",
                "limited_note",
                "exhausted",
                "partial",
                "pending",
            )
        )
        == int(coverage_register.get("inventory_count", 0) or 0),
    }
    packet = {
        "packet_kind": "literature_map",
        "mapper_version": "0.10.0",
        "algorithm_version": LITERATURE_ALGORITHM_VERSION,
        "cluster_ids": [row["cluster_id"] for row in registry["clusters"]],
        "gap_ids": [row["gap_id"] for row in gaps],
        "counts": manifest,
        "not_method_ready_bundle": True,
        "not_manuscript_text": True,
    }
    partial_cluster_ids = sorted(
        cluster_id
        for cluster_id, synthesis in cluster_syntheses.items()
        if synthesis.get("status") == "partial"
    )
    if partial_cluster_ids:
        packet.update(
            {
                "status": "partial",
                "partial_reason": "incomplete_cluster_synthesis:"
                + ",".join(partial_cluster_ids),
                "partial_cluster_ids": partial_cluster_ids,
            }
        )
    else:
        packet["status"] = "complete"
    return {
        "manifest": manifest,
        "profiles": normalized,
        "relations": navigation["typed_relations"],
        "topic_neighborhoods": navigation["topic_neighborhoods"],
        "navigation": navigation,
        "propositions": mapped_propositions,
        "study_lineages": independence["study_lineages"],
        "independence_assessments": independence["independence_assessments"],
        "evidence_base_groups": independence["evidence_base_groups"],
        "cluster_registry": {
            **registry,
            "rejected_proposals": clustered["rejected_proposals"],
            "component_actions": clustered.get("component_actions", []),
            "unclustered_sources": clustered["unclustered_sources"],
            "max_cluster_memberships": clustered["max_cluster_memberships"],
        },
        "evidence_matrices": matrices,
        "cluster_syntheses": cluster_syntheses,
        "cluster_source_contributions": {
            cluster_id: list(synthesis.get("source_contributions", []) or [])
            for cluster_id, synthesis in cluster_syntheses.items()
        },
        "quantitative_comparisons": quantitative_comparisons,
        "locator_audit": locator_audit,
        "coverage_register": coverage_register,
        "debate_registry": debates,
        "gap_registry": {
            "allowed_rules": list(GAP_RULES),
            "gaps": gaps,
            "rejected_candidates": rejected_gaps,
            "merge_ledger": gap_merge_ledger,
        },
        "gap_memory": gap_memory,
        "internal_search_log": search_log,
        "packet": packet,
    }


def _markdown_with_frontmatter(frontmatter: Mapping[str, Any], body: str) -> str:
    yaml_text = yaml.safe_dump(
        dict(frontmatter),
        sort_keys=False,
        allow_unicode=True,
        width=10_000,
    ).strip()
    return f"---\n{yaml_text}\n---\n\n{body.strip()}\n"


def _bounded_display_label(value: Any, *, fallback: str, limit: int = 56) -> str:
    label = safe_filename(str(value or "").replace("_", " "), fallback=fallback)
    label = label[:1].upper() + label[1:]
    if len(label) <= limit:
        return label
    shortened = label[:limit].rsplit(" ", 1)[0].rstrip(" .-:;")
    return f"{shortened or label[:limit].rstrip(' .-:;')}…"


def cluster_display_title(cluster: Mapping[str, Any]) -> str:
    label = _bounded_display_label(
        cluster.get("display_label")
        or cluster.get("label")
        or cluster.get("semantic_identity"),
        fallback="Unnamed Cluster",
        limit=100,
    )
    if label == label.casefold():
        label = label.title()
    return f"Cluster: {label}"


def gap_display_title(gap: Mapping[str, Any]) -> str:
    label = _bounded_display_label(
        gap.get("title")
        or gap.get("gap_statement")
        or gap.get("precise_missing_evidence")
        or gap.get("topic"),
        fallback=str(gap.get("rule") or "Collection Gap").replace("_", " ").title(),
        limit=110,
    )
    return f"Gap: {label}"


def cluster_note_stem(cluster: Mapping[str, Any]) -> str:
    label = cluster_display_title(cluster).removeprefix("Cluster: ")
    return f"Cluster - {label} [{cluster['cluster_id']}]"


def gap_note_stem(gap: Mapping[str, Any]) -> str:
    label = gap_display_title(gap).removeprefix("Gap: ")
    return f"Gap - {label} [{gap['gap_id']}]"


def _cluster_wikilink(cluster: Mapping[str, Any], *, label: str | None = None) -> str:
    display = (
        (label or cluster_display_title(cluster)).replace("|", "-").replace("]", "")
    )
    return f"[[{cluster_note_stem(cluster)}|{display}]]"


def _gap_wikilink(gap: Mapping[str, Any], *, label: str | None = None) -> str:
    display = (label or gap_display_title(gap)).replace("|", "-").replace("]", "")
    return f"[[{gap_note_stem(gap)}|{display}]]"


def _clear_generated_markdown(directory: Path) -> None:
    for path in directory.glob("*.md"):
        if path.name != "INDEX.md":
            path.unlink()


def _prune_stale_generated_markdown(
    directory: Path, *, keep_names: Sequence[str]
) -> None:
    """Remove superseded projections only after the complete render set is known."""

    keep = {str(name) for name in keep_names}
    for path in directory.glob("*.md"):
        if path.name != "INDEX.md" and path.name not in keep:
            path.unlink()


def _cluster_projection_is_publishable(synthesis: Mapping[str, Any]) -> bool:
    """Only a synthesis that passed the quality gate may replace a cluster note."""

    return (
        str(synthesis.get("status") or "") in {"reasoned", "deterministic_fallback"}
        and str(synthesis.get("quality_status") or "") == "complete"
    )


def _write_markdown_with_quality_ratchet(
    path: Path,
    text: str,
    *,
    publishable: bool,
) -> bool:
    """Write a projection unless that would replace a valid note with partial work."""

    if not publishable and path.is_file():
        return False
    atomic_write_text(path, text)
    return True


def _obsidian_note_link(row: Mapping[str, Any]) -> str:
    note_path = str(row.get("note_path") or "")
    target = (
        Path(note_path).stem
        if note_path
        else str(row.get("note_id") or row.get("source_id") or "")
    )
    title = str(row.get("title") or "").replace("|", "-").replace("]", "")
    return f"[[{target}|{title}]]" if title and title != target else f"[[{target}]]"


def _cluster_obsidian_tags(cluster: Mapping[str, Any]) -> list[str]:
    tags = [
        f"literature-cluster/{slugify(str(cluster.get('label') or cluster.get('semantic_identity') or 'cluster'))}"
    ]
    facet_priority = {
        "concept": 0,
        "mechanism": 1,
        "outcome": 2,
        "theory": 3,
        "case": 4,
        "population": 5,
        "geography": 6,
        "method": 7,
        "measure": 8,
        "data": 9,
        "period": 10,
    }
    neighborhoods = sorted(
        (
            row
            for row in cluster.get("topic_neighborhoods", []) or []
            if isinstance(row, Mapping) and row.get("canonical_tag")
        ),
        key=lambda row: (
            facet_priority.get(str(row.get("facet_type") or ""), 99),
            -int(row.get("cluster_member_count", 0) or 0),
            str(row.get("canonical_tag") or ""),
        ),
    )
    tags.extend(str(row["canonical_tag"]) for row in neighborhoods[:5])
    tags.extend(str(tag) for tag in cluster.get("subject_tags", []) or [] if str(tag))
    return list(dict.fromkeys(tags))


def _gap_obsidian_tags(
    gap: Mapping[str, Any],
    *,
    profile_by_source: Mapping[str, Mapping[str, Any]],
    cluster_by_id: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    del profile_by_source, cluster_by_id
    return sorted({str(tag) for tag in gap.get("subject_tags", []) or [] if str(tag)})


def _cluster_markdown_legacy(
    cluster: Mapping[str, Any],
    matrix: Mapping[str, Any] | None,
    debate: Mapping[str, Any] | None,
    related_gaps: Sequence[Mapping[str, Any]] = (),
    *,
    synthesis: Mapping[str, Any] | None = None,
    profile_by_source: Mapping[str, Mapping[str, Any]] | None = None,
) -> str:
    synthesis = synthesis or {}
    profile_by_source = profile_by_source or {}

    def item_text(row: Mapping[str, Any]) -> str:
        return _cluster_item_text(row) or "Evidence-backed mapped statement."

    def evidence_text(reference: Mapping[str, Any]) -> str:
        profile = profile_by_source.get(
            str(reference.get("source_id") or ""), reference
        )
        return (
            f"{_obsidian_note_link(profile)} — `{reference.get('claim_id', '')}` — "
            f"{reference.get('locator', '')}"
        )

    gaps_by_anchor: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for gap in related_gaps:
        for anchor in gap.get("anchors", []) or []:
            if str(anchor.get("cluster_id") or "") != str(
                cluster.get("cluster_id") or ""
            ):
                continue
            gaps_by_anchor[
                (str(anchor.get("section") or ""), str(anchor.get("item_id") or ""))
            ].append(gap)

    def gap_callout(gap: Mapping[str, Any]) -> list[str]:
        assessment = _as_mapping(gap.get("value_assessment"))
        design = _as_mapping(gap.get("study_design"))
        label = gap_display_title(gap).removeprefix("Gap: ")
        puzzle = str(assessment.get("puzzle") or gap.get("gap_statement") or "").strip()
        payoff = str(
            assessment.get("decision_or_inference_changed")
            or gap.get("why_matters")
            or ""
        ).strip()
        test = str(design.get("research_question") or "").strip()
        strategy = str(design.get("identification_or_inference_strategy") or "").strip()
        if strategy:
            test = f"{test} {strategy}".strip()
        return [
            "",
            f"> [!question] {_gap_wikilink(gap, label=f'Research opportunity: {label}')}",
            f"> **Puzzle:** {puzzle}",
            f"> **Payoff:** {payoff}",
            f"> **Test:** {test}",
        ]

    def render_items(
        values: Sequence[Mapping[str, Any]],
        *,
        section: str,
        include_plain_english: bool = False,
    ) -> str:
        lines: list[str] = []
        for row in values:
            lines.append(f"- {item_text(row)}")
            plain = str(
                row.get("plain_english_meaning") or row.get("plain_english") or ""
            ).strip()
            if include_plain_english and plain:
                lines.append(f"  - Plain English: {plain}")
            technical = str(
                row.get("technical_result")
                or row.get("technical_detail")
                or row.get("technical_context")
                or row.get("statistics")
                or ""
            ).strip()
            if technical:
                lines.append(f"  - Technical context: {technical}")
            references = list(row.get("evidence", []) or [])
            for reference in references[:3]:
                lines.append(f"  - Evidence: {evidence_text(reference)}")
            if len(references) > 3:
                lines.append(
                    f"  - Additional located evidence: {len(references) - 3} references in the canonical matrix."
                )
            for gap in gaps_by_anchor.get((section, str(row.get("item_id") or "")), []):
                lines.extend(gap_callout(gap))
        return "\n".join(lines) or "- No locator-backed statements were admitted."

    def fallback_synthesis() -> str:
        lines = [
            str(
                cluster.get("coherence_rationale")
                or cluster.get("shared_question")
                or ""
            ),
            "",
            "The locator-backed source claims are kept side by side because the collection does not support a stronger cross-source consensus or debate:",
        ]
        for source_id in cluster.get("source_ids", []) or []:
            profile = profile_by_source.get(str(source_id), {})
            for claim in list(profile.get("claims", []) or [])[:2]:
                text = str(claim.get("text") or claim.get("claim") or "").strip()
                if not text or not claim.get("locator"):
                    continue
                lines.append(
                    f"- {_obsidian_note_link(profile)}: {text} — `{claim.get('claim_id', '')}` — {claim.get('locator', '')}"
                )
        return "\n".join(lines).strip()

    sources = (
        "\n".join(
            f"- {_obsidian_note_link(row)} — `{row.get('source_id', '')}`"
            for row in cluster.get("representative_sources", []) or []
        )
        or "- None"
    )
    source_links = [
        _obsidian_note_link(row)
        for row in cluster.get("representative_sources", []) or []
    ]
    anchored_gap_ids = {
        str(gap.get("gap_id") or "")
        for gap in related_gaps
        for anchor in gap.get("anchors", []) or []
        if str(anchor.get("cluster_id") or "") == str(cluster.get("cluster_id") or "")
    }
    gap_links = [
        _gap_wikilink(row)
        for row in related_gaps
        if str(row.get("gap_id") or "") in anchored_gap_ids
    ]
    matrix_lines = [
        "| Dimension | Mapped values | Representative evidence |",
        "|---|---|---|",
    ]
    matrix_dimensions = (matrix or {}).get("dimensions", {})
    for dimension in EVIDENCE_DIMENSIONS:
        entries = list(matrix_dimensions.get(dimension, []) or [])
        values = [
            re.sub(r"\s+", " ", str(entry.get("value") or "")).strip()
            for entry in entries
        ]
        values = [value for value in values if value]
        displayed_values = values[:3]
        values_text = "; ".join(displayed_values) or "No locator-backed values"
        if len(values) > len(displayed_values):
            values_text += f"; +{len(values) - len(displayed_values)} more"
        representative = next(
            (
                reference
                for entry in entries
                for reference in entry.get("evidence", []) or []
                if reference.get("source_id")
                and reference.get("claim_id")
                and reference.get("locator")
            ),
            None,
        )
        if representative is not None:
            representative_profile = profile_by_source.get(
                str(representative.get("source_id") or ""), {}
            )
            evidence = (
                f"{representative_profile.get('title') or representative.get('source_id', '')} — "
                f"`{representative.get('claim_id', '')}` — {representative.get('locator', '')}"
            )
        else:
            evidence = "No representative locator"
        escaped_values = values_text.replace("|", "\\|")
        escaped_evidence = evidence.replace("|", "\\|")
        matrix_lines.append(
            f"| {str(dimension).title()} | {escaped_values} | {escaped_evidence} |"
        )
    matrix_lines.extend(
        [
            "",
            "The complete locator-level matrix is preserved in [evidence_matrices.yml](../evidence_matrices.yml) for agent use.",
        ]
    )
    matrix_text = "\n".join(matrix_lines)
    classification = str((debate or {}).get("classification") or "no_debate")
    synthesis_evidence = (
        "\n".join(
            f"- {evidence_text(reference)}"
            for reference in synthesis.get("supporting_evidence", []) or []
        )
        or "- No reasoned narrative was admitted; use the evidence-backed sections below."
    )
    frontmatter = {
        "type": "literature_cluster",
        "title": cluster_display_title(cluster),
        "aliases": [str(cluster["cluster_id"]), str(cluster["label"])],
        "cluster_id": cluster["cluster_id"],
        "semantic_identity": cluster["semantic_identity"],
        "status": cluster["status"],
        "qualification_status": cluster.get("qualification_status", cluster["status"]),
        "promoted": bool(cluster.get("promoted", True)),
        "automation_status": cluster.get("automation_status", "promoted"),
        "revision_hash": cluster["revision_hash"],
        "synthesis_status": synthesis.get("status", "deterministic_fallback"),
        "debate_classification": classification,
        "tags": _cluster_obsidian_tags(cluster),
        "sources": source_links,
        "related_gaps": gap_links,
    }
    boundaries = (
        "\n".join(f"- {value}" for value in synthesis.get("boundaries", []) or [])
        or "- No additional boundary was established."
    )
    synthesis_narrative = (
        str(synthesis.get("synthesis") or "").strip() or fallback_synthesis()
    )
    synthesis_narrative = re.sub(r"(?m)^#{1,6}\s+", "", synthesis_narrative).strip()
    body = (
        f"# {cluster_display_title(cluster)}\n\n"
        f"## Scope and Boundaries\n\n{str(synthesis.get('scope') or cluster['shared_question'])}\n\n{boundaries}\n\n"
        f"## Why These Sources Form a Cluster\n\n{synthesis.get('coherence_rationale') or cluster.get('coherence_rationale', '')}\n\n"
        f"## Synthesis\n\n{synthesis_narrative}\n\n"
        f"### Evidence for the Synthesis\n\n{synthesis_evidence}\n\n"
        f"## Cluster Status\n\n- Sources: {cluster['source_count']}\n"
        f"- Independent study families: {cluster['independent_study_family_count']}\n"
        f"- Debate classification: {classification}\n"
        f"- Formation route: {cluster.get('formation_route', 'deterministic_fallback')}\n"
        f"- Synthesis status: {synthesis.get('status', 'deterministic_fallback')}\n\n"
        f"## Central Findings\n\n{render_items(synthesis.get('central_findings', []) or [], section='central_findings', include_plain_english=True)}\n\n"
        f"## Agreements and Mapped Consensus\n\n{render_items(synthesis.get('agreements', []) or [], section='agreements')}\n\n"
        f"## Debate Positions\n\n{render_items(synthesis.get('positions', []) or [], section='positions')}\n\n"
        f"## Contradictions\n\n{render_items(synthesis.get('contradictions', []) or [], section='contradictions')}\n\n"
        f"## Boundary Conditions\n\n{render_items(synthesis.get('boundary_conditions', []) or [], section='boundary_conditions')}\n\n"
        f"## Methodological and Measurement Fault Lines\n\n{render_items(synthesis.get('methodological_fault_lines', []) or [], section='methodological_fault_lines')}\n\n"
        f"## Relationships to Neighboring Clusters\n\n{render_items(synthesis.get('related_clusters', []) or [], section='related_clusters')}\n\n"
        f"## Evidence Matrix\n\n{matrix_text}\n\n"
        f"## Source Roles\n\n{render_items(synthesis.get('source_roles', []) or [], section='source_roles')}\n\n"
        f"## Source Index\n\n{sources}"
    )
    return _markdown_with_frontmatter(frontmatter, body)


def _cluster_markdown_v09(
    cluster: Mapping[str, Any],
    matrix: Mapping[str, Any] | None,
    debate: Mapping[str, Any] | None,
    related_gaps: Sequence[Mapping[str, Any]] = (),
    *,
    synthesis: Mapping[str, Any] | None = None,
    profile_by_source: Mapping[str, Mapping[str, Any]] | None = None,
) -> str:
    """Render the human projection; full validation traces stay in YAML."""

    synthesis = synthesis or {}
    profile_by_source = profile_by_source or {}
    source_links = [
        _obsidian_note_link(row)
        for row in cluster.get("representative_sources", []) or []
    ]
    frontmatter = {
        "type": "literature_cluster",
        "title": cluster_display_title(cluster),
        "aliases": [str(cluster["cluster_id"]), str(cluster.get("label") or "")],
        "cluster_id": cluster["cluster_id"],
        "revision_hash": cluster.get("revision_hash", ""),
        "status": cluster.get("status", ""),
        "qualification_status": cluster.get("qualification_status", ""),
        "debate_state": str((debate or {}).get("classification") or "no_debate"),
        "tags": _cluster_obsidian_tags(cluster),
        "sources": source_links,
        "related_gaps": [_gap_wikilink(gap) for gap in related_gaps],
    }
    sections: list[str] = [f"# {cluster_display_title(cluster)}"]

    def citation_text(values: Sequence[Mapping[str, Any]], *, limit: int = 6) -> str:
        citations: list[str] = []
        for reference in values:
            profile = profile_by_source.get(
                str(reference.get("source_id") or ""), reference
            )
            locator = str(reference.get("locator") or "").strip()
            citation = _obsidian_note_link(profile) + (
                f" — {locator}" if locator else ""
            )
            if citation not in citations:
                citations.append(citation)
        return "; ".join(citations[:limit])

    def item_text(row: Mapping[str, Any]) -> str:
        return _human_projection_text(
            row.get("assertion")
            or row.get("finding")
            or row.get("position")
            or row.get("agreement")
            or row.get("contradiction")
            or row.get("text")
            or row.get("summary")
            or ""
        )

    question = _human_projection_text(cluster.get("shared_question") or "")
    verdict_rows = [
        _as_mapping(row)
        for row in synthesis.get("verdict_paragraphs", []) or []
        if isinstance(row, Mapping) and row.get("text")
    ]
    if not verdict_rows:
        verdict_rows = [
            {"text": central_text, "evidence": row.get("evidence", [])}
            for row in synthesis.get("central_findings", []) or []
            if isinstance(row, Mapping) and (central_text := _cluster_item_text(row))
        ]
    if verdict_rows and synthesis.get("status", "reasoned") == "reasoned":
        content: list[str] = []
        if question:
            content.append(f"**Question:** {question}")
        for row in verdict_rows:
            paragraph = re.sub(
                r"(?m)^#{1,6}\s+", "", _human_projection_text(row.get("text") or "")
            ).strip()
            if not paragraph:
                continue
            paragraph_citations = citation_text(row.get("evidence", []) or [])
            content.append(
                paragraph
                + (f" — Sources: {paragraph_citations}" if paragraph_citations else "")
            )
        content.append(
            "**Evidence basis:** "
            f"{int(cluster.get('source_count', 0) or 0)} publications; "
            f"{int(cluster.get('effective_evidence_base_count', 0) or 0)} effective evidence bases."
        )
        sections.append("## Question and verdict\n\n" + "\n\n".join(content))
    elif question and synthesis.get("status") == "deterministic_fallback":
        proposition_statements = [
            _human_projection_text(row.get("statement") or "").strip(" .;:")
            for row in cluster.get("propositions", []) or []
            if _human_projection_text(row.get("statement") or "")
        ]
        relationship_state = str(
            (debate or {}).get("classification") or "no_debate"
        ).replace("_", " ")
        proposition_summary = (
            "; ".join(proposition_statements)
            if proposition_statements
            else "the connected source-specific findings shown below"
        )
        content = [
            f"**Question:** {question}",
            (
                f"**Bounded verdict:** The collection supports treating these sources as one analytical family around "
                f"{proposition_summary}. Its strongest validated relationship is **{relationship_state}**. "
                "The source-specific findings remain separate because the evidence does not support a stronger "
                "cross-source conclusion."
            ),
            (
                "**Evidence basis:** "
                f"{int(cluster.get('source_count', 0) or 0)} publications; "
                f"{int(cluster.get('effective_evidence_base_count', 0) or 0)} effective evidence bases."
            ),
        ]
        sections.append("## Question and bounded verdict\n\n" + "\n\n".join(content))
    elif question:
        sections.append("## Cluster question\n\n" + question)

    role_by_source = {
        str(row.get("source_id") or ""): str(row.get("role") or "context")
        for row in cluster.get("source_roles", []) or []
    }
    role_lines: list[str] = []
    for role in ("core", "context", "bridge"):
        members = [
            row
            for row in cluster.get("representative_sources", []) or []
            if role_by_source.get(
                str(row.get("source_id") or ""),
                str(row.get("cluster_role") or "context"),
            )
            == role
        ]
        if not members:
            continue
        role_lines.append(f"**{role.title()} sources**")
        role_lines.extend(f"- {_obsidian_note_link(row)}" for row in members)
    if role_lines:
        sections.append("## Source roles\n\n" + "\n".join(role_lines))

    proposition_lines: list[str] = []
    for proposition in cluster.get("propositions", []) or []:
        statement = _human_projection_text(proposition.get("statement") or "")
        if not statement:
            continue
        proposition_lines.append(f"### {statement}")
        question_text = _human_projection_text(proposition.get("question") or "")
        if question_text and question_text != statement:
            proposition_lines.append(question_text)
        evidence = list(proposition.get("evidence", []) or [])
        citations = []
        for reference in evidence:
            profile = profile_by_source.get(
                str(reference.get("source_id") or ""), reference
            )
            citation = (
                f"{_obsidian_note_link(profile)} — {reference.get('locator', '')}"
            )
            if citation not in citations:
                citations.append(citation)
        if citations:
            proposition_lines.append("Evidence: " + "; ".join(citations[:5]))
    if proposition_lines:
        sections.append(
            "## Comparable propositions\n\n" + "\n\n".join(proposition_lines)
        )

    def render_assertions(
        values: Sequence[Mapping[str, Any]], *, plain_english: bool = False
    ) -> str:
        lines: list[str] = []
        for row in values:
            text = item_text(row)
            if not text:
                continue
            citations = citation_text(row.get("evidence", []) or [])
            lines.append(
                f"- {text}" + (f" — Sources: {citations}" if citations else "")
            )
            plain = _human_projection_text(
                row.get("plain_english_meaning") or row.get("plain_english") or ""
            )
            if plain_english and plain:
                lines.append(f"  - In plain English: {plain}")
            technical = _human_projection_text(
                row.get("technical_result")
                or row.get("technical_detail")
                or row.get("technical_context")
                or row.get("statistics")
                or ""
            )
            if technical:
                lines.append(f"  - Technical detail: {technical}")
        return "\n".join(lines)

    contribution_parts: list[str] = []
    contributions_by_source: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for contribution in synthesis.get("source_contributions", []) or []:
        if isinstance(contribution, Mapping) and contribution.get("source_id"):
            contributions_by_source[str(contribution["source_id"])].append(contribution)
    for source_id in cluster.get("source_ids", []) or []:
        source_id = str(source_id)
        rows = contributions_by_source.get(source_id, [])
        if not rows:
            continue
        profile = profile_by_source.get(source_id, {})
        role = role_by_source.get(source_id, "context")
        contribution_parts.append(
            f"### {_obsidian_note_link(profile)} — {role.title()}"
        )
        for row in rows:
            text = item_text(row)
            if not text:
                continue
            comparison = str(
                row.get("comparison_status") or "source_specific_not_compared"
            ).replace("_", " ")
            citations = citation_text(row.get("evidence", []) or [])
            line = f"- {text}"
            if citations:
                line += f" — Source: {citations}"
            contribution_parts.append(line)
            plain = _human_projection_text(
                row.get("plain_english_meaning") or row.get("plain_english") or ""
            )
            if plain:
                contribution_parts.append(f"  - In plain English: {plain}")
            technical = _human_projection_text(
                row.get("technical_result")
                or row.get("technical_detail")
                or row.get("technical_context")
                or row.get("statistics")
                or ""
            )
            if technical:
                contribution_parts.append(f"  - Technical detail: {technical}")
            contribution_parts.append(f"  - Comparative status: {comparison}.")
    if contribution_parts:
        sections.append(
            "## What each source contributes\n\n"
            "These findings are retained because they matter to the cluster. A source-specific contribution is not "
            "treated as cross-study agreement unless it also passes the proposition and independence gates.\n\n"
            + "\n".join(contribution_parts)
        )

    central = render_assertions(
        synthesis.get("central_findings", []) or [], plain_english=True
    )
    if central:
        sections.append("## Findings and interpretation\n\n" + central)

    debate_state = str(
        (debate or {}).get("classification")
        or synthesis.get("debate_state")
        or "no_debate"
    )
    relationship_explanations = {
        "mapped_consensus": "At least three effective evidence bases support a comparable conclusion.",
        "emerging_convergence": "Two effective evidence bases point in the same direction; this is convergence, not mature consensus.",
        "aligned_institutional_guidance": "Institutional guidance aligns, but recommendations are not independent effectiveness evidence.",
        "within_program_consistency": "Multiple publications from one evidence program are consistent; they count as one effective evidence base.",
        "conditional_relationship": "The relationship changes across identified cases, populations, periods, measures, or conditions.",
        "complementary_positions": "The sources answer different but compatible parts of the cluster question.",
        "parallel_literatures": "The propositions sit near each other but are not directly comparable.",
    }
    relationship_parts: list[str] = [
        f"**Mapped relationship:** {debate_state.replace('_', ' ')}."
        + (
            f" {relationship_explanations[debate_state]}"
            if debate_state in relationship_explanations
            else ""
        )
    ]
    for title, key in (
        ("Agreements", "agreements"),
        ("Positions", "positions"),
        ("Contradictions", "contradictions"),
    ):
        rendered = render_assertions(synthesis.get(key, []) or [])
        if rendered:
            relationship_parts.append(f"### {title}\n\n{rendered}")
    if len(relationship_parts) > 1 or debate_state != "no_debate":
        sections.append(
            "## Relationship among the findings\n\n" + "\n\n".join(relationship_parts)
        )

    strict_checks: list[str] = []
    adjudications = list((debate or {}).get("strict_adjudications", []) or [])
    if not adjudications:
        adjudications = list(synthesis.get("strict_adjudications", []) or [])
    for adjudication in adjudications:
        if not isinstance(adjudication, Mapping):
            continue
        kind = str(adjudication.get("kind") or "claim").replace("_", " ").title()
        decision = str(adjudication.get("decision") or "not_established").replace(
            "_", " "
        )
        strict_checks.append(f"### {kind}: {decision}")
        explanation = _human_projection_text(adjudication.get("explanation") or "")
        if explanation:
            strict_checks.append(explanation)
        failed_checks = [
            _as_mapping(check)
            for check in adjudication.get("checks", []) or []
            if isinstance(check, Mapping) and not bool(check.get("passed"))
        ]
        passed_checks = [
            _as_mapping(check)
            for check in adjudication.get("checks", []) or []
            if isinstance(check, Mapping) and bool(check.get("passed"))
        ]
        if passed_checks:
            strict_checks.append("**Requirements met:**")
            strict_checks.extend(
                f"- {_human_projection_text(check.get('requirement') or '')} "
                f"{_human_projection_text(check.get('explanation') or '')}".strip()
                for check in passed_checks
            )
        if failed_checks:
            strict_checks.append("**Requirements not met:**")
            strict_checks.extend(
                f"- {_human_projection_text(check.get('requirement') or '')} "
                f"{_human_projection_text(check.get('explanation') or '')}".strip()
                for check in failed_checks
            )
        what_changes = _human_projection_text(
            adjudication.get("what_would_change") or ""
        )
        if what_changes:
            strict_checks.append(
                "**What would change this assessment:** " + what_changes
            )
    if strict_checks:
        sections.append("## Strict claim checks\n\n" + "\n\n".join(strict_checks))

    boundary_parts: list[str] = []
    boundaries = render_assertions(synthesis.get("boundary_conditions", []) or [])
    methods = render_assertions(synthesis.get("methodological_fault_lines", []) or [])
    if boundaries:
        boundary_parts.append("### Boundaries\n\n" + boundaries)
    if methods:
        boundary_parts.append("### Method and measurement differences\n\n" + methods)
    if boundary_parts:
        sections.append("## Why findings differ\n\n" + "\n\n".join(boundary_parts))

    neighboring = render_assertions(synthesis.get("related_clusters", []) or [])
    if neighboring:
        sections.append("## Neighboring clusters\n\n" + neighboring)

    neighborhood_lines: list[str] = []
    for neighborhood in cluster.get("topic_neighborhoods", []) or []:
        label = _human_projection_text(neighborhood.get("label") or "")
        facet = str(neighborhood.get("facet_type") or "subject").replace("_", " ")
        canonical_tag = str(neighborhood.get("canonical_tag") or "")
        member_count = int(neighborhood.get("cluster_member_count", 0) or 0)
        if not label:
            continue
        tag_link = f" #{canonical_tag}" if canonical_tag else ""
        neighborhood_lines.append(
            f"- **{facet.title()}: {label}** — shared by {member_count} core sources in this cluster.{tag_link}"
        )
    if neighborhood_lines:
        sections.append(
            "## Related literature\n\n"
            "These are retrieval paths shared by multiple core sources. They help you browse the vault; "
            "they did not determine admission to this analytical cluster.\n\n"
            + "\n".join(neighborhood_lines)
        )

    gap_lines = []
    for gap in related_gaps:
        statement = _human_projection_text(
            gap.get("gap_statement") or gap.get("precise_missing_evidence") or ""
        )
        gap_lines.append(
            f"- {_gap_wikilink(gap)}" + (f" — {statement}" if statement else "")
        )
    if gap_lines:
        sections.append("## Collection gaps\n\n" + "\n".join(gap_lines))

    matrix_rows = list((matrix or {}).get("propositions", []) or [])
    if matrix_rows:
        table = ["| Proposition | Core-source findings |", "|---|---|"]
        for proposition in matrix_rows:
            statement = re.sub(
                r"\s+", " ", _human_projection_text(proposition.get("statement") or "")
            ).strip()
            cells = []
            for source_id, cell in _as_mapping(proposition.get("cells")).items():
                profile = profile_by_source.get(str(source_id), {})
                source_label = str(profile.get("title") or source_id)
                finding = re.sub(
                    r"\s+",
                    " ",
                    _human_projection_text(cell.get("stance_or_finding") or ""),
                ).strip()
                cells.append(f"{source_label}: {finding}")
            table.append(
                "| "
                + statement.replace("|", "\\|")
                + " | "
                + "<br>".join(cells).replace("|", "\\|")
                + " |"
            )
        sections.append("## Proposition matrix\n\n" + "\n".join(table))

    source_index = [
        f"- {_obsidian_note_link(row)}"
        for row in cluster.get("representative_sources", []) or []
    ]
    if source_index:
        sections.append("## Source index\n\n" + "\n".join(source_index))
    return _markdown_with_frontmatter(frontmatter, "\n\n".join(sections))


def _cluster_markdown(
    cluster: Mapping[str, Any],
    matrix: Mapping[str, Any] | None,
    debate: Mapping[str, Any] | None,
    related_gaps: Sequence[Mapping[str, Any]] = (),
    *,
    rejected_gap_candidates: Sequence[Mapping[str, Any]] = (),
    synthesis: Mapping[str, Any] | None = None,
    profile_by_source: Mapping[str, Mapping[str, Any]] | None = None,
    cluster_by_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> str:
    """Render the concise researcher-facing projection; detailed audits remain in YAML."""

    del matrix
    synthesis = synthesis or {}
    debate = debate or {}
    profile_by_source = profile_by_source or {}
    cluster_by_id = cluster_by_id or {}
    representative_sources = list(cluster.get("representative_sources", []) or [])
    source_links = [_obsidian_note_link(row) for row in representative_sources]
    related_cluster_links = [
        _cluster_wikilink(cluster_by_id[target_id])
        for relationship in synthesis.get("related_clusters", []) or []
        if isinstance(relationship, Mapping)
        and (
            target_id := str(
                relationship.get("target_cluster_id")
                or relationship.get("related_cluster_id")
                or relationship.get("cluster_id")
                or ""
            )
        )
        in cluster_by_id
    ]
    frontmatter = {
        "type": "literature_cluster",
        "title": cluster_display_title(cluster),
        "cluster_id": str(cluster.get("cluster_id") or ""),
        "tags": _cluster_obsidian_tags(cluster),
        "sources": source_links,
        "related_gaps": [_gap_wikilink(gap) for gap in related_gaps],
        "related_clusters": list(dict.fromkeys(related_cluster_links)),
    }

    def citations(references: Sequence[Mapping[str, Any]]) -> str:
        values: list[str] = []
        for reference in references:
            source_id = str(reference.get("source_id") or "")
            profile = profile_by_source.get(source_id, reference)
            locator = _human_locator_text(reference.get("locator") or "")
            value = _obsidian_note_link(profile)
            if locator:
                value += f" — {locator}"
            if value not in values:
                values.append(value)
        return "; ".join(values)

    def contribution_locators(references: Sequence[Mapping[str, Any]]) -> str:
        """Avoid repeating a source wikilink already used as the bullet label."""

        values: list[str] = []
        for reference in references:
            locator = _human_locator_text(reference.get("locator") or "")
            if locator and locator not in values:
                values.append(locator)
        return "; ".join(values)

    def narrative_text(row: Mapping[str, Any]) -> str:
        return _human_projection_text(
            row.get("assertion")
            or row.get("finding")
            or row.get("agreement")
            or row.get("position")
            or row.get("contradiction")
            or row.get("boundary")
            or row.get("fault_line")
            or row.get("relationship")
            or row.get("summary")
            or row.get("text")
            or ""
        )

    def proposition_assessment_explanation(assessment: Mapping[str, Any]) -> str:
        state = str(assessment.get("state") or "no_debate")
        detail = _as_mapping(assessment.get("explanation"))
        evidence_bases = int(detail.get("effective_evidence_base_count", 0) or 0)
        publications = int(detail.get("publication_count", 0) or 0)
        explanations = {
            "mapped_consensus": (
                "At least three independent evidence bases reach the same conclusion "
                "on a comparable claim."
            ),
            "emerging_convergence": (
                f"{evidence_bases or 'Several'} independent evidence base"
                f"{'s' if evidence_bases != 1 else ''} point in the same direction, "
                "but the evidence is not broad enough for strong consensus."
            ),
            "within_program_consistency": (
                f"{publications or 'Several'} publications point in the same direction, "
                "but they draw on one underlying evidence base and therefore do not "
                "provide independent confirmation."
            ),
            "mapped_debate": (
                "Comparable evidence bases reach genuinely opposing conclusions on this claim."
            ),
            "conditional_relationship": (
                "The result differs across the populations, cases, periods, or conditions "
                "used by the studies, so one unconditional conclusion is not justified."
            ),
            "mixed_evidence": (
                "The available findings are not sufficiently aligned or comparable to "
                "support one conclusion."
            ),
            "complementary_positions": (
                "The sources address different but compatible parts of this claim rather "
                "than testing one common effect."
            ),
            "aligned_institutional_guidance": (
                "The guidance documents recommend similar practices, but recommendations "
                "do not demonstrate effectiveness."
            ),
            "parallel_literatures": (
                "The sources are topically related but do not test the same claim."
            ),
            "single_position": (
                "Only one evidence base addresses this precise claim in the collection."
            ),
            "no_debate": "No comparable multi-source position was located for this claim.",
        }
        return explanations.get(state, explanations["no_debate"])

    def gap_rejection_explanation(reasons: Sequence[str]) -> str:
        reason_set = {str(reason) for reason in reasons}
        resolution_fields = {
            "missing_resolution_path_comparison": "comparison group",
            "missing_resolution_path_estimand": "estimand",
            "missing_resolution_path_feasibility": "feasibility evidence",
            "missing_resolution_path_identification": "identification strategy",
            "missing_resolution_path_measurement": "measurement plan",
        }
        missing_resolution = [
            label for code, label in resolution_fields.items() if code in reason_set
        ]
        explanations: list[str] = []
        if missing_resolution:
            explanations.append(
                "it did not specify a complete resolution path, including "
                + ", ".join(missing_resolution)
            )
        if "missing_value_assessment_puzzle_type" in reason_set:
            explanations.append(
                "the non-obvious research puzzle was not classified precisely enough"
            )
        if {
            "missing_originating_proposition",
            "missing_originating_cluster_revision",
        } & reason_set:
            explanations.append(
                "its lineage did not resolve to a named cluster proposition and revision"
            )
        if "missing_locator_backed_generation_evidence" in reason_set:
            explanations.append("the generating evidence was not fully locator-backed")
        if "missing_evidence_not_specific" in reason_set:
            explanations.append(
                "the missing evidence was too broad to distinguish a useful answer"
            )
        if not explanations:
            return "The candidate did not pass the collection-level gap gate."
        return (
            "The candidate was not promoted because "
            + "; and ".join(explanations)
            + "."
        )

    def strict_claim_explanation(adjudication: Mapping[str, Any]) -> str:
        """Translate deterministic gate failures into concise researcher prose."""

        kind = str(adjudication.get("kind") or "claim")
        details: list[str] = []
        failed_requirements = [
            str(check.get("requirement") or "").casefold()
            for check in adjudication.get("checks", []) or []
            if isinstance(check, Mapping) and not bool(check.get("passed"))
        ]
        comparability_failed = any(
            "comparable proposition" in requirement
            or "same comparable" in requirement
            for requirement in failed_requirements
        )
        for check in adjudication.get("checks", []) or []:
            if not isinstance(check, Mapping) or bool(check.get("passed")):
                continue
            requirement = str(check.get("requirement") or "").casefold()
            explanation = _human_projection_text(check.get("explanation") or "")
            if "comparable proposition" in requirement or "same comparable" in requirement:
                detail = (
                    "The sources address related parts of the question, but they do not test the same directly comparable claim."
                )
            elif "three independent evidence bases" in requirement:
                if comparability_failed:
                    continue
                count_match = re.search(r"\b(\d+)\b", explanation)
                count = count_match.group(1) if count_match else "fewer than three"
                noun = "independent evidence base" if count == "1" else "independent evidence bases"
                detail = (
                    f"Only {count} {noun} support the same claim; strong consensus requires at least three."
                )
            elif "outcomes, populations, concepts" in requirement:
                detail = (
                    "The studies use different outcomes, populations, concepts, or comparisons, so their results cannot be pooled into one conclusion."
                )
            elif "align rather than conflict" in requirement:
                detail = (
                    "The findings vary by context or make complementary contributions instead of supporting one unconditional conclusion."
                )
            elif "opposing positions" in requirement:
                detail = (
                    "No two comparable sources reach genuinely opposite conclusions on the same claim."
                )
            else:
                detail = explanation
            if detail and detail not in details:
                details.append(detail)
        if not details:
            explanation = _human_projection_text(
                adjudication.get("explanation") or ""
            )
            if explanation:
                details.append(explanation)
        if kind == "contradiction" and not any(
            "opposite" in detail.casefold() for detail in details
        ):
            details.append(
                "No two comparable sources reach genuinely opposite conclusions on the same claim."
            )
        return " ".join(details)

    def render_rows(rows: Sequence[Mapping[str, Any]]) -> list[str]:
        rendered: list[str] = []
        for row in rows:
            text = narrative_text(row)
            if not text:
                continue
            source_text = citations(row.get("evidence", []) or [])
            rendered.append(f"- {text}" + (f" — {source_text}" if source_text else ""))
            plain = _human_projection_text(
                row.get("plain_english_meaning") or row.get("plain_english") or ""
            )
            if plain and plain.casefold() not in text.casefold():
                rendered.append(f"  - Put simply: {plain}")
        return rendered

    question = _cluster_display_question(cluster, synthesis)
    synthesis_text = _cluster_answer_excerpt(synthesis, cluster=cluster)
    if not synthesis_text:
        synthesis_text = " ".join(
            " ".join(
                value
                for value in (
                    narrative_text(row),
                    _human_projection_text(
                        row.get("plain_english_meaning")
                        or row.get("plain_english")
                        or ""
                    ),
                )
                if value
            )
            for row in synthesis.get("central_findings", []) or []
            if narrative_text(row)
        )
    answer_parts = [f"**Evidence base:** {_cluster_researcher_status(cluster)}"]
    if synthesis_text:
        answer_parts.append(synthesis_text)
    else:
        answer_parts.append(
            "A complete evidence-grounded synthesis has not yet passed the publication-quality gate."
        )
    display_role_by_source = _cluster_display_source_roles(cluster, synthesis)
    core_count = sum(role == "core" for role in display_role_by_source.values())
    answer_parts.append(
        f"This cluster draws on {core_count} core source{'s' if core_count != 1 else ''}."
    )
    sections = [f"# {cluster_display_title(cluster)}"]
    if question:
        sections.append("## Research question\n\n" + question)
    sections.append("## Verdict\n\n" + "\n\n".join(answer_parts))

    coherence = _cluster_display_coherence(cluster, synthesis)
    thread_rows = _cluster_display_threads(cluster, synthesis)
    fit_lines = [coherence] if coherence else []
    if thread_rows:
        thread_titles = [
            _human_projection_text(row.get("title") or row.get("question") or "")
            for row in thread_rows
        ]
        thread_titles = [value for value in thread_titles if value]
        if thread_titles:
            fit_lines.append(
                "The literature is organized around "
                + ", ".join(thread_titles[:-1])
                + (
                    f", and {thread_titles[-1]}"
                    if len(thread_titles) > 1
                    else thread_titles[0]
                )
                + "."
            )
    role_by_source = display_role_by_source
    role_links: dict[str, list[str]] = defaultdict(list)
    for source in representative_sources:
        source_id = str(source.get("source_id") or "")
        role_links[
            role_by_source.get(source_id, str(source.get("cluster_role") or "context"))
        ].append(_obsidian_note_link(source))
    if fit_lines:
        sections.append(
            "## Why these studies form a cluster\n\n" + "\n\n".join(fit_lines)
        )
    role_sections: list[str] = []
    for role, label in (
        ("core", "Core studies"),
        ("context", "Context sources"),
        ("bridge", "Bridge sources"),
    ):
        if role_links.get(role):
            role_sections.append(f"**{label}:** " + "; ".join(role_links[role]))
    if role_sections:
        sections.append("## Sources and their roles\n\n" + "\n\n".join(role_sections))

    all_contributions = [
        dict(row)
        for row in synthesis.get("source_contributions", []) or []
        if isinstance(row, Mapping)
    ]
    thread_ids = {str(row.get("thread_id") or "") for row in thread_rows}
    contribution_kind_priority = {
        "direct_proposition_finding": 0,
        "unique_cluster_relevant_finding": 1,
        "boundary_evidence": 2,
        "methodological_context": 3,
        "conceptual_context": 4,
        "bridge_evidence": 5,
    }
    ranked_contributions = sorted(
        all_contributions,
        key=lambda row: (
            not _contribution_display_is_complete(row),
            _contribution_substantive_penalty(cluster, row),
            str(row.get("origin") or "reasoner") != "reasoner",
            -_cluster_row_relevance(cluster, row)[0],
            str(row.get("evidence_thread_id") or "") not in thread_ids,
            contribution_kind_priority.get(str(row.get("contribution_kind") or ""), 9),
            -len(_human_projection_text(row.get("finding") or "")),
            str(row.get("contribution_id") or ""),
        ),
    )
    best_by_source: dict[str, dict[str, Any]] = {}
    for row in ranked_contributions:
        source_id = str(row.get("source_id") or "")
        if source_id and source_id not in best_by_source:
            best_by_source[source_id] = row
    source_order = [str(value) for value in cluster.get("source_ids", []) or []]
    core_contributions = [
        best_by_source[source_id]
        for source_id in source_order
        if source_id in best_by_source and role_by_source.get(source_id) == "core"
    ]
    context_contributions = [
        best_by_source[source_id]
        for source_id in source_order
        if source_id in best_by_source and role_by_source.get(source_id) != "core"
    ][:2]
    contributions = [*core_contributions, *context_contributions]
    contributions_by_thread: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in contributions:
        contributions_by_thread[str(row.get("evidence_thread_id") or "")].append(row)

    findings: list[str] = []
    seen_meanings: set[str] = set()
    central_rows = [
        dict(row)
        for row in synthesis.get("central_findings", []) or []
        if isinstance(row, Mapping) and narrative_text(row)
    ]
    if not thread_rows:
        if central_rows:
            findings.append("### Overall pattern")
            findings.extend(render_rows(central_rows))
    used_contribution_ids: set[str] = set()
    for thread in thread_rows:
        thread_id = str(thread.get("thread_id") or "")
        title = _human_projection_text(
            thread.get("title") or thread.get("question") or "Evidence thread"
        )
        findings.append(f"### {title}")
        summary = _human_projection_text(thread.get("summary") or "")
        plain = _human_projection_text(thread.get("plain_english_meaning") or "")
        thread_citations = citations(thread.get("evidence", []) or [])
        if summary:
            findings.append(
                summary + (f" — {thread_citations}" if thread_citations else "")
            )
        if plain and plain.casefold() not in summary.casefold():
            findings.append(f"**In plain English:** {plain}")
        for contribution in contributions_by_thread.get(thread_id, []):
            contribution_id = str(
                contribution.get("contribution_id") or _stable_hash(contribution)
            )
            used_contribution_ids.add(contribution_id)
            source_id = str(contribution.get("source_id") or "")
            source = profile_by_source.get(source_id, {"source_id": source_id})
            finding = _map_verdict_excerpt(
                contribution.get("finding"), sentence_limit=2, character_limit=650
            )
            reference = contribution_locators(
                contribution.get("evidence", []) or []
            )
            findings.append(
                f"- **{_obsidian_note_link(source)}:** {finding}"
                + (f" — {reference}" if reference else "")
            )
            technical = _human_technical_result(
                contribution.get("technical_result") or ""
            )
            if technical and technical.casefold() not in {
                "not reported",
                "not_reported",
            }:
                findings.append(f"  - Technical result: {technical}")
            meaning = _map_verdict_excerpt(
                contribution.get("plain_english_meaning"),
                sentence_limit=2,
                character_limit=450,
            )
            if meaning and meaning.casefold() not in seen_meanings:
                findings.append(f"  - What it means: {meaning}")
                seen_meanings.add(meaning.casefold())
    unassigned = [
        row
        for row in contributions
        if str(row.get("contribution_id") or _stable_hash(row))
        not in used_contribution_ids
    ]
    if unassigned:
        findings.append("### Additional important findings")
        for contribution in unassigned:
            source_id = str(contribution.get("source_id") or "")
            source = profile_by_source.get(source_id, {"source_id": source_id})
            finding = _map_verdict_excerpt(
                contribution.get("finding"), sentence_limit=2, character_limit=650
            )
            reference = contribution_locators(
                contribution.get("evidence", []) or []
            )
            findings.append(
                f"- **{_obsidian_note_link(source)}:** {finding}"
                + (f" — {reference}" if reference else "")
            )
            technical = _human_technical_result(
                contribution.get("technical_result") or ""
            )
            if technical and technical.casefold() not in {
                "not reported",
                "not_reported",
            }:
                findings.append(f"  - Technical result: {technical}")
            meaning = _map_verdict_excerpt(
                contribution.get("plain_english_meaning"),
                sentence_limit=2,
                character_limit=450,
            )
            if meaning and meaning.casefold() not in seen_meanings:
                findings.append(f"  - What it means: {meaning}")
                seen_meanings.add(meaning.casefold())
    if findings:
        sections.append("## What the studies find\n\n" + "\n\n".join(findings))
    if thread_rows and central_rows:
        sections.append(
            "## Connections across the findings\n\n"
            + "\n".join(render_rows(central_rows))
        )

    relationship_state = str(
        debate.get("classification") or synthesis.get("debate_state") or "no_debate"
    )
    relationship_explanations = {
        "mapped_consensus": "At least three independent evidence bases support the same comparable conclusion.",
        "emerging_convergence": "Several independent studies point in the same direction, but the evidence is not yet broad enough to call this strong consensus.",
        "aligned_institutional_guidance": "The guidance documents recommend similar practices, but recommendations are not evidence that those practices cause better outcomes.",
        "within_program_consistency": "Several publications from the same research program reach compatible conclusions, so they do not count as independent confirmation.",
        "conditional_relationship": "The findings vary across contexts or conditions described below.",
        "complementary_positions": "The sources answer different but compatible parts of the cluster question.",
        "parallel_literatures": (
            "The studies are grouped because they address different parts of the cluster question; "
            "their findings should be read separately rather than as tests of one common claim."
        ),
        "mixed_evidence": "The findings are not sufficiently aligned or comparable to support one conclusion.",
        "single_position": "The collection contains one located position on the relevant claim.",
        "mapped_debate": "Comparable sources support genuinely different conclusions.",
        "no_debate": "The collection does not contain a directly comparable disagreement on this question.",
    }
    relation_lines = [
        relationship_explanations.get(
            relationship_state, relationship_explanations["no_debate"]
        )
    ]
    for assessment in debate.get("proposition_assessments", []) or []:
        if not isinstance(assessment, Mapping):
            continue
        statement = _human_projection_text(assessment.get("statement") or "")
        if statement:
            relation_lines.append(
                f"- **On {statement}:** "
                + proposition_assessment_explanation(assessment)
            )
    for key in ("agreements", "positions", "contradictions"):
        relation_lines.extend(render_rows(synthesis.get(key, []) or []))
    for adjudication in list(debate.get("strict_adjudications", []) or []) or list(
        synthesis.get("strict_adjudications", []) or []
    ):
        if not isinstance(adjudication, Mapping):
            continue
        kind = str(adjudication.get("kind") or "claim").replace("_", " ")
        candidate = _map_verdict_excerpt(
            adjudication.get("candidate"), sentence_limit=1, character_limit=280
        )
        label = {
            "consensus": "Strong consensus",
            "contradiction": "A direct contradiction",
        }.get(kind, kind[:1].upper() + kind[1:])
        subject = f"{label} for “{candidate}”" if candidate else label
        decision = str(adjudication.get("decision") or "not_established")
        explanation = strict_claim_explanation(adjudication)
        if decision == "established":
            relation_lines.append(f"- **{subject} is established:** {explanation}")
        else:
            relation_lines.append(f"- **{subject} is not established:** {explanation}")
    sections.append(
        "## Consensus, disagreement, and uncertainty\n\n"
        + "\n".join(relation_lines)
    )

    limit_lines: list[str] = []
    removed_scope_terms = [
        str(value).casefold()
        for value in cluster.get("removed_restrictive_scope_terms", []) or []
        if str(value)
    ]
    if cluster.get("display_scope_note"):
        limit_lines.append(
            f"- {_human_projection_text(cluster.get('display_scope_note'))}"
        )
    limit_lines.extend(
        f"- {_human_projection_text(value)}"
        for value in synthesis.get("boundaries", []) or []
        if _human_projection_text(value)
        and not (
            removed_scope_terms
            and any(term in str(value).casefold() for term in removed_scope_terms)
            and re.search(
                r"\b(?:only|excludes?|limited to|restricted to)\b",
                str(value),
                re.I,
            )
        )
    )
    limit_lines.extend(render_rows(synthesis.get("boundary_conditions", []) or []))
    limit_lines.extend(
        render_rows(synthesis.get("methodological_fault_lines", []) or [])
    )
    if limit_lines:
        sections.append(
            "## Boundary, method, and measurement differences\n\n"
            + "\n".join(limit_lines)
        )

    related_cluster_lines: list[str] = []
    for relationship in synthesis.get("related_clusters", []) or []:
        if not isinstance(relationship, Mapping):
            continue
        target_id = str(
            relationship.get("target_cluster_id")
            or relationship.get("related_cluster_id")
            or relationship.get("cluster_id")
            or ""
        )
        target = cluster_by_id.get(target_id)
        text = _human_projection_text(
            relationship.get("relationship")
            or relationship.get("explanation")
            or relationship.get("summary")
            or ""
        )
        if target is None or not text:
            continue
        evidence = [
            *list(relationship.get("current_evidence", []) or []),
            *list(relationship.get("target_evidence", []) or []),
        ]
        source_text = citations(evidence)
        related_cluster_lines.append(
            f"- {_cluster_wikilink(target)} — {text}"
            + (f" — {source_text}" if source_text else "")
        )
    if related_cluster_lines:
        sections.append(
            "## Related clusters\n\n" + "\n".join(related_cluster_lines)
        )

    if related_gaps:
        sections.append(
            "## Collection gaps\n\n"
            + "\n".join(
                f"- {_gap_wikilink(gap)} — "
                + (
                    "Collection gap lead: "
                    if str(gap.get("status") or "") == "collection_gap_lead"
                    else "Mapped collection gap: "
                )
                + _human_projection_text(
                    gap.get("gap_statement")
                    or gap.get("precise_missing_evidence")
                    or ""
                )
                for gap in related_gaps
            )
        )
    considered_gap_lines: list[str] = []
    for gap in rejected_gap_candidates[:3]:
        statement = _human_projection_text(
            gap.get("gap_statement")
            or gap.get("precise_missing_evidence")
            or gap.get("missing_evidence")
            or ""
        )
        reasons = [
            str(value)
            for value in (
                gap.get("quality_rejection_reasons")
                or gap.get("specificity_errors")
                or []
            )
            if str(value)
        ]
        decision = (
            gap_rejection_explanation(reasons)
            if reasons
            else _human_projection_text(
                gap.get("internal_search_summary")
                or gap.get("decision_reasoning")
                or "The candidate did not pass the collection-level gap gate."
            )
        )
        if statement:
            considered_gap_lines.append(
                f"- **Not promoted:** {statement}"
                + (f" Why: {decision.rstrip('.')}." if decision else "")
            )
    if considered_gap_lines:
        sections.append(
            "## Why other gap candidates were not retained\n\n"
            "These collection-native candidates were examined but did not meet the visible-gap standard.\n\n"
            + "\n".join(considered_gap_lines)
        )
    if source_links:
        sections.append(
            "## Source index\n\n"
            + "\n".join(f"- {value}" for value in source_links)
        )
    return _markdown_with_frontmatter(frontmatter, "\n\n".join(sections))


def _gap_markdown_legacy(
    gap: Mapping[str, Any],
    *,
    profile_by_source: Mapping[str, Mapping[str, Any]] | None = None,
    cluster_by_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> str:
    profile_by_source = profile_by_source or {}
    cluster_by_id = cluster_by_id or {}

    def evidence_line(row: Mapping[str, Any]) -> str:
        profile = profile_by_source.get(str(row.get("source_id") or ""), row)
        claim = next(
            (
                value
                for value in profile.get("claims", []) or []
                if str(value.get("claim_id") or "") == str(row.get("claim_id") or "")
            ),
            {},
        )
        claim_text = str(claim.get("text") or "").strip()
        return (
            f"- {_obsidian_note_link(profile)} — `{row.get('claim_id', '')}` — "
            f"{row.get('locator', '')}"
            f"{f' — {claim_text}' if claim_text else ''}"
        )

    support = (
        "\n".join(
            evidence_line(row) for row in gap.get("supporting_evidence", []) or []
        )
        or "- None"
    )
    counter = (
        "\n".join(
            evidence_line(row) for row in gap.get("countervailing_evidence", []) or []
        )
        or "- None"
    )
    limited_warnings = [
        row
        for row in gap.get("warnings", []) or []
        if row.get("warning") == "possible_counterevidence_requires_full_text"
    ]
    warning_lines = (
        "\n".join(
            f"- {_obsidian_note_link(profile_by_source.get(str(row.get('source_id') or ''), row))} — full text required"
            for row in limited_warnings
        )
        or "- None"
    )
    source_ids = {
        str(row.get("source_id") or "")
        for field in ("supporting_evidence", "countervailing_evidence")
        for row in gap.get(field, []) or []
        if row.get("source_id")
    }
    source_ids.update(
        str(row.get("source_id") or "")
        for row in limited_warnings
        if row.get("source_id")
    )
    source_links = [
        _obsidian_note_link(profile_by_source[source_id])
        for source_id in sorted(source_ids)
        if source_id in profile_by_source
    ]
    related_clusters = [
        cluster_by_id[str(cluster_id)]
        for cluster_id in gap.get("related_cluster_ids", []) or []
        if str(cluster_id) in cluster_by_id
    ]
    cluster_links = [_cluster_wikilink(cluster) for cluster in related_clusters]
    cluster_lines = (
        "\n".join(f"- {_cluster_wikilink(cluster)}" for cluster in related_clusters)
        or "- No canonical cluster relation recorded."
    )
    result = (gap.get("rule_results") or [{}])[0]
    search_counts = Counter(
        str(row.get("status") or "")
        for row in gap.get("internal_search_results", []) or []
    )
    search_terms = (
        ", ".join(f"`{value}`" for value in gap.get("internal_search_terms", []) or [])
        or "None"
    )
    search_lines = (
        "\n".join(
            f"- {status.replace('_', ' ')}: {count}"
            for status, count in sorted(search_counts.items())
        )
        or "- No search results were recorded."
    )
    closest_lines = (
        "\n".join(
            f"- {_obsidian_note_link(row)} — confidence {row.get('confidence', 0)} — {row.get('overlap_explanation', '')}"
            for row in gap.get("closest_prior_work", []) or []
        )
        or "- No semantically close analytical source was identified."
    )
    assessment = _as_mapping(gap.get("value_assessment"))
    design = _as_mapping(gap.get("study_design"))

    def list_text(values: Any) -> str:
        rows = [str(value).strip() for value in values or [] if str(value).strip()]
        return "\n".join(f"- {value}" for value in rows) or "- None specified."

    frontmatter = {
        "type": "literature_gap",
        "title": gap_display_title(gap),
        "aliases": [str(gap["gap_id"]), gap_display_title(gap).removeprefix("Gap: ")],
        "gap_id": gap["gap_id"],
        "rule": gap["rule"],
        "status": gap["status"],
        "scope": "collection_only",
        "promoted": bool(gap["promoted"]),
        "automation_status": gap.get("automation_status", "lead"),
        "novelty_claimed": False,
        "rationale_status": gap.get("rationale_status", "deterministic_fallback"),
        "quality_gate_passed": bool(gap.get("quality_gate_passed", False)),
        "priority_tier": gap.get("priority_tier", ""),
        "structured_signature": gap.get("structured_signature", ""),
        "merged_from_gap_ids": list(gap.get("merged_from_gap_ids", []) or []),
        "tags": _gap_obsidian_tags(
            gap, profile_by_source=profile_by_source, cluster_by_id=cluster_by_id
        ),
        "sources": source_links,
        "related_clusters": cluster_links,
    }
    body = (
        f"# {gap_display_title(gap)}\n\n"
        f"## Gap Statement\n\n{gap.get('gap_statement') or gap['precise_missing_evidence']}\n\n"
        f"## Status and Scope\n\n- Rule: {gap['rule'].replace('_', ' ').title()}\n"
        f"- Status: {str(gap.get('status') or '').replace('_', ' ')}\n"
        f"- Scope: frozen collection only\n- Novelty beyond the collection: not claimed\n\n"
        f"## How the System Identified This Gap\n\n{gap.get('generation_explanation', '')}\n\n"
        f"## Observed Evidence Pattern\n\n{gap.get('observed_pattern', '')}\n\n"
        f"## Precise Missing Evidence\n\n{gap['precise_missing_evidence']}\n\n"
        f"## Why This Is Not an Obvious Gap\n\n"
        f"**Puzzle:** {assessment.get('puzzle', '')}\n\n"
        f"**Strongest obvious answer:** {assessment.get('strongest_obvious_answer', '')}\n\n"
        f"**Why that answer is inadequate:** {assessment.get('why_obvious_answer_is_inadequate', '')}\n\n"
        f"**Competing explanations:**\n\n{list_text(assessment.get('competing_explanations', []))}\n\n"
        f"**What resolving it changes:** {assessment.get('decision_or_inference_changed', '')}\n\n"
        f"**Expected information gain:** {assessment.get('information_gain', '')}\n\n"
        f"## Automated Rule Result\n\n- Decision: {result.get('decision', '')}\n"
        f"- Independent study families: {result.get('independent_study_families', 0)}\n"
        f"- Locator completeness: {result.get('locator_completeness', 0)}\n"
        f"- Analytical profiles searched: {result.get('analytical_profile_count_searched', 0)}\n\n"
        f"## Related Clusters\n\n{cluster_lines}\n\n"
        f"## Supporting Sources and Locators\n\n{support}\n\n"
        f"## Collection-Wide Internal Search\n\nSearch terms: {search_terms}\n\n{search_lines}\n\n"
        f"{gap.get('internal_search_summary', '')}\n\n"
        f"## Closest Prior Work in the Collection\n\n{closest_lines}\n\n{gap.get('closest_prior_explanation', '')}\n\n"
        f"## Countervailing Sources and Locators\n\n{counter}\n\n"
        f"## Possible Counterevidence Requiring Full Text\n\n{warning_lines}\n\n"
        f"## Why the Candidate Survived or Was Narrowed\n\n{gap.get('decision_reasoning', '')}\n\n"
        f"## Executable Study Design\n\n"
        f"- Design: {design.get('design_type', '')}\n"
        f"- Research question: {design.get('research_question', '')}\n"
        f"- Estimand: {design.get('estimand', '')}\n"
        f"- Unit of analysis: {design.get('unit_of_analysis', '')}\n"
        f"- Target population: {design.get('target_population', '')}\n"
        f"- Exposure or treatment: {design.get('exposure_or_treatment', '')}\n"
        f"- Comparator: {design.get('comparator', '')}\n"
        f"- Identification or inference strategy: {design.get('identification_or_inference_strategy', '')}\n"
        f"- Data route: {design.get('data_route', '')}\n"
        f"- Feasibility: {design.get('feasibility', '')}\n"
        f"- Ethical constraints: {design.get('ethical_constraints', '')}\n\n"
        f"### Outcomes\n\n{list_text(design.get('outcomes', []))}\n\n"
        f"### Mechanism Measures\n\n{list_text(design.get('mechanism_measures', []))}\n\n"
        f"### Confounders and Rival Explanations\n\n{list_text(design.get('confounders_or_rival_explanations', []))}\n\n"
        f"### Falsification and Process Tests\n\n{list_text(design.get('falsification_or_process_tests', []))}\n\n"
        f"### Validity Risks\n\n{list_text(design.get('validity_risks', []))}\n\n"
        f"## Evidence Needed to Resolve It\n\n{gap.get('evidence_needed', '')}\n\n"
        f"## Why It Matters\n\n{gap['why_matters']}\n\n"
        f"## Contribution\n\n{gap['contribution']}"
    )
    return _markdown_with_frontmatter(frontmatter, body)


def _gap_markdown(
    gap: Mapping[str, Any],
    *,
    profile_by_source: Mapping[str, Mapping[str, Any]] | None = None,
    cluster_by_id: Mapping[str, Mapping[str, Any]] | None = None,
) -> str:
    """Render a collection-scoped rationale without exposing audit machinery."""

    profile_by_source = profile_by_source or {}
    cluster_by_id = cluster_by_id or {}
    related_clusters = [
        cluster_by_id[str(cluster_id)]
        for cluster_id in gap.get("related_cluster_ids", []) or []
        if str(cluster_id) in cluster_by_id
    ]
    source_ids = {
        str(reference.get("source_id") or "")
        for field in ("supporting_evidence", "countervailing_evidence")
        for reference in gap.get(field, []) or []
        if reference.get("source_id")
    }
    source_links = [
        _obsidian_note_link(profile_by_source[source_id])
        for source_id in sorted(source_ids)
        if source_id in profile_by_source
    ]
    frontmatter = {
        "type": "literature_gap",
        "title": gap_display_title(gap),
        "aliases": [str(gap["gap_id"]), gap_display_title(gap).removeprefix("Gap: ")],
        "gap_id": gap["gap_id"],
        "rule": gap.get("rule", ""),
        "status": gap.get("status", ""),
        "scope": "collection_only",
        "promoted": bool(gap.get("promoted", False)),
        "novelty_claimed": False,
        "related_clusters": [
            _cluster_wikilink(cluster) for cluster in related_clusters
        ],
        "sources": source_links,
        "tags": _gap_obsidian_tags(
            gap, profile_by_source=profile_by_source, cluster_by_id=cluster_by_id
        ),
    }
    sections: list[str] = [f"# {gap_display_title(gap)}"]

    statement = _human_projection_text(
        gap.get("gap_statement") or gap.get("precise_missing_evidence") or ""
    )
    if statement:
        sections.append("## Gap statement\n\n" + statement)
    sections.append(
        "## Collection status\n\n"
        f"This is a **{str(gap.get('status') or '').replace('_', ' ')}** inside the frozen collection. "
        "It is not a claim that the wider literature contains no answer."
    )

    lineage_lines = [
        f"- Related cluster: {_cluster_wikilink(cluster)}"
        for cluster in related_clusters
    ]
    proposition_ids = {str(value) for value in gap.get("proposition_ids", []) or []}
    for cluster in related_clusters:
        for proposition in cluster.get("propositions", []) or []:
            if str(proposition.get("proposition_id") or "") in proposition_ids:
                lineage_lines.append(
                    f"- Originating proposition: {_human_projection_text(proposition.get('statement', ''))}"
                )
    missing_cell = _as_mapping(gap.get("missing_cell"))
    if missing_cell.get("description"):
        lineage_lines.append(
            f"- Missing evidence-matrix cell: {_human_projection_text(missing_cell.get('description'))}"
        )
    if lineage_lines:
        sections.append("## Where the gap came from\n\n" + "\n".join(lineage_lines))

    generation = _human_projection_text(gap.get("generation_explanation") or "")
    if re.fullmatch(
        r"Generated by [a-z_ ]+ rule from cluster_synthesis\.?",
        generation,
        flags=re.I,
    ):
        rule_label = str(gap.get("rule") or "collection gap").replace("_", " ")
        generation = (
            f"The cluster synthesis raised this candidate under the {rule_label} rule."
        )
    observed = _map_verdict_excerpt(
        gap.get("observed_pattern") or "",
        sentence_limit=2,
        character_limit=650,
    )
    if generation or observed:
        parts = []
        if generation:
            parts.append(generation)
        if observed:
            parts.append("**Observed collection pattern:** " + observed)
        sections.append("## Why the mapper raised it\n\n" + "\n\n".join(parts))

    def evidence_lines(values: Sequence[Mapping[str, Any]]) -> list[str]:
        resolved: list[tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]] = []
        sources_with_specific_anchors: set[str] = set()
        for reference in values:
            source_id = str(reference.get("source_id") or "")
            profile = profile_by_source.get(source_id, reference)
            anchor_id = str(
                reference.get("evidence_anchor_id") or reference.get("claim_id") or ""
            )
            anchor = next(
                (
                    row
                    for row in profile.get("claims", []) or []
                    if str(row.get("evidence_anchor_id") or row.get("claim_id") or "")
                    == anchor_id
                ),
                {},
            )
            resolved.append((reference, profile, anchor))
            if anchor and not _anchor_is_composite_note_summary(anchor):
                sources_with_specific_anchors.add(source_id)

        lines: list[str] = []
        seen: set[tuple[str, str, str]] = set()
        for reference, profile, anchor in resolved:
            source_id = str(reference.get("source_id") or "")
            anchor_id = str(
                reference.get("evidence_anchor_id") or reference.get("claim_id") or ""
            )
            if (
                source_id in sources_with_specific_anchors
                and _anchor_is_composite_note_summary(anchor)
            ):
                continue
            locator = _human_locator_text(reference.get("locator") or "")
            identity = (
                source_id,
                anchor_id,
                _normalized_locator(locator),
            )
            if identity in seen:
                continue
            seen.add(identity)
            claim_text = _human_projection_text(anchor.get("text") or "")
            line = f"- {_obsidian_note_link(profile)}"
            if locator:
                line += f" — {locator}"
            if claim_text:
                line += f": {claim_text}"
            lines.append(line)
        return lines

    support = evidence_lines(gap.get("supporting_evidence", []) or [])
    if support:
        sections.append("## Evidence that reveals the gap\n\n" + "\n".join(support))

    search_parts: list[str] = []
    search_summary = _human_projection_text(gap.get("internal_search_summary") or "")
    if search_summary:
        search_parts.append(search_summary)
    closest: list[dict[str, Any]] = []
    for raw_prior in gap.get("closest_prior_work", []) or []:
        if not isinstance(raw_prior, Mapping):
            continue
        explanation = _human_projection_text(raw_prior.get("overlap_explanation") or "")
        # Deterministic token overlap is useful for machine-side search, but a
        # raw word list is not an explanation of prior work for a researcher.
        if not explanation or re.match(
            r"^(?:matched|shared) collection terms\s*:", explanation, flags=re.I
        ):
            continue
        closest.append(
            {
                **dict(raw_prior),
                "overlap_explanation": explanation,
            }
        )
    if closest:
        search_parts.append(
            "**Closest collection evidence**\n\n"
            + "\n".join(
                f"- {_obsidian_note_link(row)} — {_human_projection_text(row.get('overlap_explanation', ''))}"
                for row in closest[:5]
            )
        )
    elif gap.get("closest_prior_work"):
        search_parts.append(
            "No source in this collection was close enough to count as responsive prior evidence. "
            "The complete search record remains in the map sidecar."
        )
    closest_explanation = _human_projection_text(
        gap.get("closest_prior_explanation") or ""
    )
    if closest_explanation:
        search_parts.append(
            "**Why it does not fully answer the gap:** " + closest_explanation
        )
    if search_parts:
        sections.append(
            "## Collection-wide falsification\n\n" + "\n\n".join(search_parts)
        )

    counter = evidence_lines(gap.get("countervailing_evidence", []) or [])
    limited = [
        row
        for row in gap.get("warnings", []) or []
        if row.get("warning") == "possible_counterevidence_requires_full_text"
        and _human_projection_text(row.get("overlap_explanation") or "")
        and not re.match(
            r"^(?:matched|shared) collection terms\s*:",
            _human_projection_text(row.get("overlap_explanation") or ""),
            flags=re.I,
        )
    ]
    counter_parts: list[str] = []
    if counter:
        counter_parts.append("**Counterevidence**\n\n" + "\n".join(counter))
    if limited:
        counter_parts.append(
            "**Possible counterevidence requiring full text**\n\n"
            + "\n".join(
                f"- {_obsidian_note_link(profile_by_source.get(str(row.get('source_id') or ''), row))}"
                for row in limited
            )
        )
    if counter_parts:
        sections.append(
            "## Counterevidence and limits\n\n" + "\n\n".join(counter_parts)
        )

    decision = _human_projection_text(gap.get("decision_reasoning") or "")
    if decision:
        sections.append("## Why it survived or was narrowed\n\n" + decision)

    strict = _as_mapping(gap.get("strict_adjudication"))
    if strict:
        strict_parts = [
            f"**Decision:** {str(strict.get('decision') or 'not_established').replace('_', ' ')}",
            _human_projection_text(strict.get("explanation") or ""),
        ]
        failed_checks = [
            _as_mapping(check)
            for check in strict.get("checks", []) or []
            if isinstance(check, Mapping) and not bool(check.get("passed"))
        ]
        if failed_checks:
            strict_parts.append("**Requirements not met:**")
            strict_parts.extend(
                f"- {_human_projection_text(check.get('requirement') or '')} "
                f"{_human_projection_text(check.get('explanation') or '')}".strip()
                for check in failed_checks
            )
        what_changes = _human_projection_text(strict.get("what_would_change") or "")
        if what_changes:
            strict_parts.append(
                "**What would change this assessment:** " + what_changes
            )
        sections.append(
            "## Strong-gap threshold\n\n"
            + "\n\n".join(part for part in strict_parts if part)
        )

    resolution = _as_mapping(gap.get("resolution_path"))
    if resolution:
        path_type = str(resolution.get("path_type") or "")
        requirements = _as_mapping(resolution.get("requirements"))
        required_fields = _RESOLUTION_PATH_REQUIREMENTS.get(path_type, ())
        missing_fields = [
            str(field_name).replace("_", " ")
            for field_name in required_fields
            if not _flatten_values(requirements.get(field_name))
        ]
        resolution_valid = bool(
            path_type in _RESOLUTION_PATH_REQUIREMENTS
            and resolution.get("question")
            and resolution.get("evidence_needed")
            and not missing_fields
            and len(_tokens(resolution.get("feasibility", ""))) >= 2
        )
        path_lines = _resolution_path_summary(resolution)
        if not resolution_valid:
            caveat = "This is a provisional direction, not a validated research design."
            if missing_fields:
                caveat += (
                    " It still needs a specific " + ", ".join(missing_fields) + "."
                )
            path_lines.insert(0, caveat)
        sections.append(
            (
                "## A route to resolving it"
                if resolution_valid
                else "## A provisional route to resolving it"
            )
            + "\n\n"
            + "\n\n".join(path_lines)
        )

    final_parts = []
    if gap.get("why_matters"):
        final_parts.append("**Why it matters:** " + str(gap["why_matters"]))
    if gap.get("contribution"):
        final_parts.append("**Possible contribution:** " + str(gap["contribution"]))
    ranking = _as_mapping(gap.get("ranking"))
    if ranking.get("confidence_tier"):
        final_parts.append(
            "**Collection-level confidence:** " + str(ranking["confidence_tier"])
        )
    if final_parts:
        sections.append("## Significance\n\n" + "\n\n".join(final_parts))
    return _markdown_with_frontmatter(frontmatter, "\n\n".join(sections))


def _map_verdict_excerpt(
    value: Any, *, sentence_limit: int = 3, character_limit: int = 900
) -> str:
    human_text = _human_projection_text(value)
    human_text = re.sub(
        r"(?im)^\s{0,3}#{1,6}\s+(?:cluster\s+)?(?:synthesis|verdict)\s*$",
        "",
        human_text,
    )
    text = re.sub(r"\s+", " ", human_text).strip()
    text = re.sub(
        r"\s*\([^()]*,\s*\d{4}[a-z]?,\s*p\.\s*$",
        "",
        text,
        flags=re.I,
    ).rstrip()
    if not text:
        return ""
    sentences = _split_display_sentences(text)
    selected: list[str] = []
    for sentence in sentences[:sentence_limit]:
        candidate = " ".join([*selected, sentence])
        if len(candidate) <= character_limit:
            selected.append(sentence)
            continue
        if selected:
            break
        shortened = sentence[:character_limit]
        boundary = max(shortened.rfind(";"), shortened.rfind(","), shortened.rfind(":"))
        if boundary >= max(80, character_limit // 2):
            shortened = shortened[:boundary]
        else:
            shortened = shortened.rsplit(" ", 1)[0]
        shortened = shortened.rstrip(" ,;:-")
        if re.search(
            r"\b(?:and|or|but|with|by|for|from|in|of|to|vs\.?|p{1,2}\.)$",
            shortened,
            flags=re.I,
        ):
            shortened = shortened.rsplit(" ", 1)[0].rstrip(" ,;:-")
        return _clean_display_excerpt(shortened + ".")
    return _clean_display_excerpt(" ".join(selected))


def _split_display_sentences(text: str) -> list[str]:
    """Split prose without treating citation abbreviations as sentences."""

    sentinel = "\ue000"
    protected = str(text)
    for pattern in (
        r"\bet\s+al\.",
        r"\bpp?\.",
        r"\bvs\.",
        r"\be\.g\.",
        r"\bi\.e\.",
        r"\b(?:[A-Z]\.){2,}",
    ):
        protected = re.sub(
            pattern,
            lambda match: match.group(0).replace(".", sentinel),
            protected,
            flags=re.I if pattern in {r"\bet\s+al\.", r"\bpp?\.", r"\bvs\."} else 0,
        )
    return [
        sentence.replace(sentinel, ".").strip()
        for sentence in re.split(r"(?<=[.!?])\s+", protected)
        if sentence.strip()
    ]


def _clean_display_excerpt(text: str) -> str:
    """Remove truncated citation fragments and dangling parentheticals."""

    cleaned = str(text).strip()
    incomplete_end = re.compile(
        r"(?:\bet\s+al\.|\bpp?\.|\bvs\.|\bcovering\.?)$",
        flags=re.I,
    )
    while cleaned and incomplete_end.search(cleaned):
        sentences = _split_display_sentences(cleaned)
        if len(sentences) <= 1:
            return ""
        cleaned = " ".join(sentences[:-1]).strip()
    while cleaned.count("(") > cleaned.count(")"):
        cleaned = cleaned.rsplit("(", 1)[0].rstrip(" ,;:-")
    if cleaned and cleaned[-1] not in ".!?":
        cleaned += "."
    return cleaned


def _cluster_answer_excerpt(
    synthesis: Mapping[str, Any],
    *,
    cluster: Mapping[str, Any] | None = None,
    max_threads: int = 6,
    character_limit: int = 1_100,
) -> str:
    """Represent every principal evidence thread once in a compact answer."""

    removed_scope_terms = [
        str(value).casefold()
        for value in (cluster or {}).get("removed_restrictive_scope_terms", []) or []
        if str(value)
    ]

    def scope_safe(value: str) -> bool:
        text = value.casefold()
        return not bool(
            removed_scope_terms
            and any(term in text for term in removed_scope_terms)
            and re.search(r"\b(?:only|excludes?|limited to|restricted to)\b", text)
        )

    thread_sentences: list[str] = []
    seen_sentences: set[str] = set()
    rows = (
        _cluster_display_threads(cluster, synthesis)
        if cluster is not None
        else [
            dict(row)
            for row in synthesis.get("evidence_threads", []) or []
            if isinstance(row, Mapping)
        ]
    )
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        value = row.get("summary") or row.get("plain_english_meaning") or ""
        if not _human_projection_text(value):
            continue
        sentence = _map_verdict_excerpt(
            value,
            sentence_limit=1,
            character_limit=340,
        )
        if not scope_safe(sentence):
            continue
        identity = _canonical_phrase(sentence)
        if not sentence or identity in seen_sentences:
            continue
        seen_sentences.add(identity)
        thread_sentences.append(sentence)
        if len(thread_sentences) >= max_threads:
            break
    if not thread_sentences and not rows:
        synthesis_excerpt = _map_verdict_excerpt(
            synthesis.get("synthesis"),
            sentence_limit=4,
            character_limit=character_limit,
        )
        if synthesis_excerpt:
            thread_sentences.append(synthesis_excerpt)
    if cluster is not None and len(thread_sentences) < max_threads:
        display_roles = _cluster_display_source_roles(cluster, synthesis)
        core_source_ids = {
            source_id for source_id, role in display_roles.items() if role == "core"
        }
        covered_source_ids = {
            str(reference.get("source_id") or "")
            for row in rows
            if str(row.get("origin") or "") != "deterministic_source_contribution_map"
            for reference in row.get("evidence", []) or []
            if isinstance(reference, Mapping) and reference.get("source_id")
        }
        if not rows and thread_sentences:
            covered_source_ids.update(
                str(reference.get("source_id") or "")
                for reference in synthesis.get("supporting_evidence", []) or []
                if isinstance(reference, Mapping) and reference.get("source_id")
            )
        source_by_id = {
            str(row.get("source_id") or ""): row
            for row in cluster.get("representative_sources", []) or []
            if isinstance(row, Mapping) and row.get("source_id")
        }
        contribution_rows = sorted(
            [
                dict(row)
                for row in synthesis.get("source_contributions", []) or []
                if isinstance(row, Mapping)
                and str(row.get("source_id") or "")
                in core_source_ids - covered_source_ids
                and _contribution_display_is_complete(row)
                and _cluster_row_relevance(cluster, row)[1]
            ],
            key=lambda row: (
                _contribution_substantive_penalty(cluster, row),
                str(row.get("origin") or "reasoner") != "reasoner",
                -_cluster_row_relevance(cluster, row)[0],
                -len(_human_projection_text(row.get("finding") or "")),
                str(row.get("source_id") or ""),
            ),
        )
        used_sources: set[str] = set()
        for contribution in contribution_rows:
            source_id = str(contribution.get("source_id") or "")
            if source_id in used_sources:
                continue
            used_sources.add(source_id)
            source = source_by_id.get(source_id, {})
            note_stem = Path(str(source.get("note_path") or "")).stem
            source_label = (
                note_stem.split(" - ", 1)[0]
                if note_stem
                else str(source.get("title") or "One core source")
            )
            source_label = _human_projection_text(source_label)
            finding = _map_verdict_excerpt(
                contribution.get("finding"), sentence_limit=1, character_limit=300
            )
            if not finding:
                continue
            sentence = (
                f"{source_label} reports that {finding[:1].lower() + finding[1:]}"
            )
            if not scope_safe(sentence):
                continue
            identity = _canonical_phrase(sentence)
            if identity not in seen_sentences:
                seen_sentences.add(identity)
                thread_sentences.append(sentence)
            if len(thread_sentences) >= max_threads:
                break
    answer = " ".join(value for value in thread_sentences if value)
    if not answer:
        answer = _map_verdict_excerpt(
            synthesis.get("synthesis"),
            sentence_limit=4,
            character_limit=character_limit,
        )
    if len(answer) <= character_limit:
        return _clean_display_excerpt(answer)
    return _map_verdict_excerpt(
        answer,
        sentence_limit=max_threads,
        character_limit=character_limit,
    )


def _map_unclustered_reason_label(value: Any) -> str:
    reason = str(value or "").strip()
    normalized = reason.casefold()
    if reason == "no_comparable_multi_source_proposition":
        return "No comparable proposition shared with another core study"
    if reason == "no_comparable_independent_evidence_base_proposition":
        return "Comparable publications do not provide two effective evidence bases"
    if normalized.startswith("coverage classification:"):
        return "Limited source coverage"
    if "membership" in normalized and "limit" in normalized:
        return "Analytical-cluster membership limit"
    labels = {
        "limited_source_coverage": "Limited source coverage",
        "singleton_bounded_literature": "No second independent study addresses this bounded research problem",
        "insufficient_independent_evidence_bases": "Too few independent evidence bases for cluster admission",
        "broad_topical_overlap_only": "Related by topic, but not by a sufficiently bounded research conversation",
        "no_central_locator_backed_membership_anchor": "No locator-backed central finding supports cluster membership",
        "incomparable_research_problem": "The source addresses a materially different research problem",
        "membership_limit_exceeded": "Analytical-cluster membership limit",
    }
    if reason in labels:
        return labels[reason]
    return (
        reason.replace("_", " ").strip().capitalize() or "No admission reason recorded"
    )


def _literature_map_markdown_v09(
    report: Mapping[str, Any],
    source_set: Mapping[str, Any],
    *,
    map_id: str,
) -> str:
    """Render a collection-level intellectual overview without another model call."""

    manifest = _as_mapping(report.get("manifest"))
    cluster_registry = _as_mapping(report.get("cluster_registry"))
    clusters = list(cluster_registry.get("clusters", []) or [])
    unclustered = list(cluster_registry.get("unclustered_sources", []) or [])
    syntheses = _as_mapping(report.get("cluster_syntheses"))
    debates = {
        str(row.get("cluster_id") or ""): row
        for row in _as_mapping(report.get("debate_registry")).get("assessments", [])
        or []
        if isinstance(row, Mapping)
    }
    gaps = list(_as_mapping(report.get("gap_registry")).get("gaps", []) or [])
    coverage_register = _as_mapping(report.get("coverage_register"))
    coverage_counts = _as_mapping(coverage_register.get("counts"))
    profile_by_source = {
        str(row.get("source_id") or ""): row
        for row in report.get("profiles", []) or []
        if isinstance(row, Mapping) and row.get("source_id")
    }
    analytical_source_ids = {
        source_id
        for source_id, profile in profile_by_source.items()
        if profile.get("analytical")
    }
    clustered_analytical_source_ids = {
        str(source_id)
        for cluster in clusters
        for source_id in cluster.get("source_ids", []) or []
        if str(source_id) in analytical_source_ids
    }
    partial_cluster_ids = [
        str(cluster.get("cluster_id") or "")
        for cluster in clusters
        if _as_mapping(syntheses.get(str(cluster.get("cluster_id") or ""))).get(
            "status"
        )
        == "partial"
    ]
    collection_name = str(
        source_set.get("collection_name")
        or source_set.get("source_set_alias")
        or source_set.get("source_set_id")
        or "Collection"
    ).strip()
    map_title = f"Literature Map - {collection_name}"
    frontmatter = {
        "type": "literature_map_index",
        "title": map_title,
        "map_id": map_id,
        "source_set_id": str(source_set.get("source_set_id") or ""),
        "scope": "collection_only",
        "status": "partial" if partial_cluster_ids else "complete",
        "tags": [],
    }
    sections = [
        f"# {map_title}",
        (
            "## What this map does\n\n"
            "This is the collection-level overview of the frozen Zotero source set. Atomic notes analyze individual "
            "sources; cluster notes compare sources that address the same propositions; this map shows how those "
            "clusters, relationships, and collection-relative gaps fit together. It does not make a claim about the "
            "complete published literature."
        ),
    ]

    analytical_count = int(manifest.get("analytical_profile_count", 0) or 0)
    limited_count = int(manifest.get("limited_profile_count", 0) or 0)
    inventory_count = int(
        coverage_register.get("inventory_count", analytical_count + limited_count) or 0
    )
    exhausted_count = int(coverage_counts.get("exhausted", 0) or 0)
    partial_count = int(coverage_counts.get("partial", 0) or 0)
    pending_count = int(coverage_counts.get("pending", 0) or 0)
    cluster_count = len(clusters)
    clustered_count = len(clustered_analytical_source_ids)
    collection_verdict = (
        f"The frozen collection contains {inventory_count} inventoried items: {analytical_count} analytical profiles, "
        f"{limited_count} limited profiles, and {exhausted_count} exhausted sources. "
        f"The mapper admitted {cluster_count} analytical cluster{'s' if cluster_count != 1 else ''}; "
        f"{clustered_count} analytical source{'s' if clustered_count != 1 else ''} contribute to at least one admitted cluster. "
    )
    if cluster_count:
        collection_verdict += (
            "The remaining analytical sources stay searchable but are not treated as evidence for a shared debate "
            "unless they address a comparable multi-source proposition."
        )
    else:
        collection_verdict += (
            "No multi-source proposition met the admission threshold, so the collection currently has no defensible "
            "analytical cluster."
        )
    sections.append("## Collection verdict\n\n" + collection_verdict)

    cluster_cards: list[str] = []
    for cluster in clusters:
        cluster_id = str(cluster.get("cluster_id") or "")
        synthesis = _as_mapping(syntheses.get(cluster_id))
        debate = _as_mapping(debates.get(cluster_id))
        question = _human_projection_text(cluster.get("shared_question") or "")
        verdict = (
            _map_verdict_excerpt(synthesis.get("synthesis"))
            if synthesis.get("status") == "reasoned"
            else ""
        )
        role_by_source = {
            str(row.get("source_id") or ""): str(row.get("role") or "")
            for row in cluster.get("source_roles", []) or []
            if isinstance(row, Mapping)
        }
        core_count = sum(1 for role in role_by_source.values() if role == "core")
        cluster_cards.append(f"### {_cluster_wikilink(cluster)}")
        if question:
            cluster_cards.append(f"**Question:** {question}")
        cluster_cards.append(
            f"**Verdict:** {verdict}"
            if verdict
            else "**Verdict:** The cluster synthesis is incomplete and remains resumable."
        )
        cluster_cards.append(
            f"- Evidence base: {core_count} core sources; "
            f"{int(cluster.get('effective_evidence_base_count', 0) or 0)} effective evidence bases"
        )
        cluster_cards.append(
            "- Relationship among findings: "
            + str(
                debate.get("classification")
                or synthesis.get("debate_state")
                or "no_debate"
            ).replace("_", " ")
        )
        cluster_cards.append(
            f"- Source-specific contributions retained: {len(synthesis.get('source_contributions', []) or [])}"
        )
        related_gap_count = sum(
            1
            for gap in gaps
            if cluster_id
            in {str(value) for value in gap.get("related_cluster_ids", []) or []}
        )
        cluster_cards.append(f"- Linked collection gaps: {related_gap_count}")
    sections.append(
        "## Clusters at a glance\n\n"
        + (
            "\n\n".join(cluster_cards)
            if cluster_cards
            else "No analytical clusters were admitted in this map."
        )
    )

    relationship_lines: list[str] = []
    cluster_by_id = {
        str(cluster.get("cluster_id") or ""): cluster for cluster in clusters
    }
    for cluster in clusters:
        cluster_id = str(cluster.get("cluster_id") or "")
        synthesis = _as_mapping(syntheses.get(cluster_id))
        for relationship in synthesis.get("related_clusters", []) or []:
            if not isinstance(relationship, Mapping):
                continue
            text = _human_projection_text(
                relationship.get("relationship")
                or relationship.get("assertion")
                or relationship.get("summary")
                or relationship.get("text")
                or ""
            )
            target_id = str(
                relationship.get("target_cluster_id")
                or relationship.get("cluster_id")
                or relationship.get("related_cluster_id")
                or ""
            )
            target = cluster_by_id.get(target_id)
            if text:
                target_text = (
                    f" and {_cluster_wikilink(target)}" if target is not None else ""
                )
                relationship_lines.append(
                    f"- {_cluster_wikilink(cluster)}{target_text}: {text}"
                )
    for left, right in combinations(clusters, 2):
        shared = {str(value) for value in left.get("source_ids", []) or []} & {
            str(value) for value in right.get("source_ids", []) or []
        }
        if shared:
            relationship_lines.append(
                f"- {_cluster_wikilink(left)} and {_cluster_wikilink(right)} share "
                f"{len(shared)} source{'s' if len(shared) != 1 else ''}."
            )
    for gap in gaps:
        related_ids = [
            str(value)
            for value in gap.get("related_cluster_ids", []) or []
            if str(value) in cluster_by_id
        ]
        if len(set(related_ids)) > 1:
            relationship_lines.append(
                f"- {_gap_wikilink(gap)} connects "
                + ", ".join(
                    _cluster_wikilink(cluster_by_id[cluster_id])
                    for cluster_id in sorted(set(related_ids))
                )
                + "."
            )
    relationship_lines = list(dict.fromkeys(relationship_lines))
    if not relationship_lines:
        relationship_lines.append(
            "No evidence-backed cross-cluster relationship was established. The admitted clusters should therefore "
            "be read as separate evidence domains rather than as one combined debate."
        )
    sections.append("## How the clusters relate\n\n" + "\n".join(relationship_lines))

    gap_lines: list[str] = []
    for gap in gaps:
        statement = _human_projection_text(
            gap.get("gap_statement") or gap.get("precise_missing_evidence") or ""
        )
        status = str(gap.get("status") or "collection_gap_lead").replace("_", " ")
        gap_lines.append(
            f"- {_gap_wikilink(gap)} — {status}"
            + (f": {statement}" if statement else "")
        )
    if not gap_lines:
        gap_lines.append(
            "No gap survived the collection-wide specificity, non-obviousness, worth, and internal-falsification gates. "
            "This means no defensible collection-relative gap was established, not that the wider literature has no gaps."
        )
    sections.append("## Collection gaps\n\n" + "\n".join(gap_lines))

    reason_counts = Counter(
        _map_unclustered_reason_label(row.get("reason"))
        for row in unclustered
        if isinstance(row, Mapping)
    )
    coverage_lines = [
        f"- Frozen inventory: {inventory_count}",
        f"- Analytical profiles: {analytical_count}",
        f"- Limited profiles: {limited_count}",
        f"- Exhausted sources: {exhausted_count}",
        f"- Partial sources: {partial_count}",
        f"- Pending sources: {pending_count}",
        f"- Analytical sources represented in admitted clusters: {clustered_count}",
        f"- Unclustered sources: {len(unclustered)}",
        f"- Accounting valid: {'yes' if sum(int(coverage_counts.get(key, 0) or 0) for key in ('validated_note', 'limited_note', 'exhausted', 'partial', 'pending')) == inventory_count else 'no'}",
    ]
    exhausted_rows = [
        row
        for row in coverage_register.get("records", []) or []
        if isinstance(row, Mapping) and row.get("terminal_state") == "exhausted"
    ]
    if exhausted_rows:
        coverage_lines.extend(["", "**Exhausted-source register**"])
        coverage_lines.extend(
            f"- `{row.get('zotero_key') or row.get('source_id') or 'unknown item'}` — "
            f"{row.get('exclusion_reason') or 'source processing exhausted'}"
            for row in exhausted_rows
        )
    if reason_counts:
        coverage_lines.extend(
            ["", "**Why sources remain outside analytical clusters**"]
        )
        coverage_lines.extend(
            f"- {reason}: {count}"
            for reason, count in sorted(
                reason_counts.items(), key=lambda item: (-item[1], item[0])
            )
        )
    coverage_lines.extend(
        [
            "",
            "Unclustered does not mean irrelevant. It means a source did not provide comparable evidence for an admitted "
            "multi-source proposition in this frozen collection. Limited sources remain searchable but cannot establish "
            "substantive findings, debates, or gaps without adequate full text.",
        ]
    )
    sections.append("## Coverage and limits\n\n" + "\n".join(coverage_lines))
    sections.append(
        "## Tags and literature neighborhoods\n\n"
        "Subject tags are collection-native retrieval labels derived from the existing source profiles, such as "
        "`mechanism/mediator-legitimacy`, `outcome/mediation-success`, or `case/syria`. They let Obsidian connect "
        "notes through a shared concept, mechanism, outcome, method, or case.\n\n"
        "A literature neighborhood is promoted only when at least two independent analytical sources share the same "
        "typed subject tag. Neighborhoods are browsing aids, not analytical clusters: they cannot establish a debate, "
        "admit a cluster, or answer a gap. Single-source facets remain source-local search metadata and do not become "
        "native graph tags by default. Promoted neighborhoods appear inside each cluster's "
        "\"Related literature\" section."
    )
    sections.append(
        "## Navigate\n\n"
        "- [[clusters/INDEX|Cluster Index]] — concise navigation to the admitted clusters\n"
        "- [[gaps/INDEX|Gap Registry Index]] — collection-relative gaps and leads\n"
        "- [[02_source_memory/indexes/INDEX|Source Index]] — every generated source note"
    )
    return _markdown_with_frontmatter(frontmatter, "\n\n".join(sections))


def stable_literature_map_id(
    source_set: Mapping[str, Any], question: str | None = None
) -> str:
    """Identify a map by its stable source-set alias, never by a mutable snapshot."""
    del (
        question
    )  # A question is a projection lens, not part of collection-map identity.
    source_set_alias = str(
        source_set.get("source_set_alias")
        or source_set.get("source_set_id")
        or "source-set"
    )
    identity = {"source_set_alias": source_set_alias}
    return f"literature-map-{slugify(source_set_alias)}-{_stable_hash(identity)[:12]}"


def literature_map_note_stem(source_set: Mapping[str, Any], map_id: str) -> str:
    """Human-first filename with the stable map identity retained."""

    collection_name = str(
        source_set.get("collection_name")
        or source_set.get("source_set_alias")
        or source_set.get("source_set_id")
        or "Collection"
    ).strip()
    label = safe_filename(collection_name, fallback="Collection")[:100].rstrip(" .-")
    return f"Literature Map - {label} [{map_id}]"


def _load_map_cluster_registry(workspace: Path, map_id: str) -> Mapping[str, Any]:
    canonical = (
        workspace / "03_literature_synthesis" / "maps" / map_id / "cluster_registry.yml"
    )
    payload = read_yaml(canonical, None)
    if isinstance(payload, Mapping):
        return payload
    maps_root = workspace / "03_literature_synthesis" / "maps"
    if maps_root.exists() and any(path.is_dir() for path in maps_root.iterdir()):
        return {}
    for legacy in (
        workspace / "03_literature_synthesis" / "cluster_registry.yml",
        workspace / "03_literature_synthesis" / "clusters" / "clusters.yml",
    ):
        payload = read_yaml(legacy, None)
        if isinstance(payload, Mapping):
            return payload
    return {}


def _projection_without_volatile_timestamps(value: Any) -> Any:
    """Return projection content without recursively generated timestamps."""

    if isinstance(value, Mapping):
        return {
            str(key): _projection_without_volatile_timestamps(child)
            for key, child in value.items()
            if str(key) not in {"created_at", "updated_at"}
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_projection_without_volatile_timestamps(child) for child in value]
    return value


def _literature_map_markdown(
    report: Mapping[str, Any],
    source_set: Mapping[str, Any],
    *,
    map_id: str,
) -> str:
    """Render a compact intellectual map rather than a processing audit."""

    manifest = _as_mapping(report.get("manifest"))
    clusters = list(
        _as_mapping(report.get("cluster_registry")).get("clusters", []) or []
    )
    syntheses = _as_mapping(report.get("cluster_syntheses"))
    gap_registry = _as_mapping(report.get("gap_registry"))
    gaps = list(gap_registry.get("gaps", []) or [])
    rejected_gaps = list(gap_registry.get("rejected_candidates", []) or [])
    cluster_registry = _as_mapping(report.get("cluster_registry"))
    unclustered = [
        _as_mapping(row)
        for row in cluster_registry.get("unclustered_sources", []) or []
        if isinstance(row, Mapping)
    ]
    collection_name = str(
        source_set.get("collection_name")
        or source_set.get("source_set_alias")
        or source_set.get("source_set_id")
        or "Collection"
    ).strip()
    title = f"Literature Map - {collection_name}"
    partial_cluster_ids = [
        str(cluster.get("cluster_id") or "")
        for cluster in clusters
        if _as_mapping(syntheses.get(str(cluster.get("cluster_id") or ""))).get(
            "status"
        )
        == "partial"
    ]
    sections = [
        f"# {title}",
        (
            "## How to use this map\n\n"
            "This map organizes the frozen Zotero collection into coherent subliteratures. Cluster notes explain what the "
            "sources find, how their arguments and evidence relate, and where the collection remains uncertain. The map is "
            "collection-relative: it does not claim to cover every publication in the wider literature. Start with a cluster, "
            "follow its links to atomic notes for source-level evidence, and open a linked gap note for the full collection-native rationale."
        ),
    ]

    inventory_count = int(manifest.get("coverage_inventory_count", 0) or 0)
    analytical_count = int(manifest.get("analytical_profile_count", 0) or 0)
    limited_count = int(manifest.get("limited_profile_count", 0) or 0)
    exhausted_count = int(manifest.get("coverage_exhausted_count", 0) or 0)
    partial_count = int(manifest.get("coverage_partial_count", 0) or 0)
    pending_count = int(manifest.get("coverage_pending_count", 0) or 0)
    clustered_sources = {
        str(source_id)
        for cluster in clusters
        for source_id in cluster.get("source_ids", []) or []
    }
    profiles_by_source = {
        str(row.get("source_id") or ""): row
        for row in report.get("profiles", []) or []
        if isinstance(row, Mapping) and row.get("source_id")
    }
    coverage_lines = [
        f"- Frozen collection items: {inventory_count}",
        f"- Analytical profiles: {analytical_count}",
        f"- Limited profiles: {limited_count}",
        f"- Exhausted sources: {exhausted_count}",
        f"- Partial sources: {partial_count}",
        f"- Pending sources: {pending_count}",
        f"- Sources represented in clusters: {len(clustered_sources)}",
        f"- Analytical sources outside clusters: {len(unclustered)}",
    ]
    if unclustered:
        coverage_lines.extend(["", "**Why some analytical sources remain outside clusters**"])
        for row in unclustered:
            source_id = str(row.get("source_id") or "")
            source = profiles_by_source.get(source_id, row)
            reason = _map_unclustered_reason_label(row.get("reason"))
            detail = _human_projection_text(row.get("reason_detail") or "")
            coverage_lines.append(
                f"- {_obsidian_note_link(source)} — {reason}"
                + (f". {detail}" if detail and detail.casefold() not in reason.casefold() else ".")
            )
    sections.append("## Collection coverage\n\n" + "\n".join(coverage_lines))

    cards: list[str] = []
    for cluster in clusters:
        cluster_id = str(cluster.get("cluster_id") or "")
        synthesis = _as_mapping(syntheses.get(cluster_id))
        question = _cluster_display_question(cluster, synthesis)
        verdict = _cluster_answer_excerpt(
            synthesis, cluster=cluster, max_threads=3, character_limit=850
        )
        coherence = _map_verdict_excerpt(
            _cluster_display_coherence(cluster, synthesis),
            sentence_limit=1,
            character_limit=360,
        )
        cards.append(f"### {_cluster_wikilink(cluster)}")
        if question:
            cards.append(f"**Central question:** {question}")
        cards.append(f"**Evidence base:** {_cluster_researcher_status(cluster)}")
        if verdict:
            cards.append(verdict)
        else:
            cards.append(
                "The cluster has defensible membership, but its full narrative synthesis is still pending."
            )
        if coherence and coherence.casefold() not in verdict.casefold():
            cards.append(f"**Why these sources belong together:** {coherence}")
        display_roles = _cluster_display_source_roles(cluster, synthesis)
        core_count = sum(role == "core" for role in display_roles.values())
        cards.append(
            f"*Evidence base: {core_count} core source{'s' if core_count != 1 else ''}.*"
        )
    sections.append(
        "## Literature clusters\n\n"
        + (
            "\n\n".join(cards)
            if cards
            else "No coherent multi-source cluster was admitted."
        )
    )

    cluster_by_id = {str(row.get("cluster_id") or ""): row for row in clusters}
    display_roles_by_cluster = {
        str(cluster.get("cluster_id") or ""): _cluster_display_source_roles(
            cluster,
            _as_mapping(syntheses.get(str(cluster.get("cluster_id") or ""))),
        )
        for cluster in clusters
    }
    relationships: list[str] = []
    seen_relationships: set[str] = set()
    seen_relationship_pairs: set[tuple[str, str]] = set()
    for cluster in clusters:
        synthesis = _as_mapping(syntheses.get(str(cluster.get("cluster_id") or "")))
        for relationship in synthesis.get("related_clusters", []) or []:
            if not isinstance(relationship, Mapping):
                continue
            target_id = str(
                relationship.get("cluster_id")
                or relationship.get("related_cluster_id")
                or ""
            )
            text = _human_projection_text(
                relationship.get("relationship")
                or relationship.get("summary")
                or relationship.get("text")
                or ""
            )
            target = cluster_by_id.get(target_id)
            if not text or target is None:
                continue
            pair = tuple(sorted((str(cluster.get("cluster_id") or ""), target_id)))
            if pair in seen_relationship_pairs:
                continue
            signature = _stable_hash(
                sorted([str(cluster.get("cluster_id") or ""), target_id]) + [text]
            )
            if signature in seen_relationships:
                continue
            seen_relationships.add(signature)
            seen_relationship_pairs.add(pair)
            relationships.append(
                f"- {_cluster_wikilink(cluster)} and {_cluster_wikilink(target)}: {text}"
            )
    for left, right in combinations(clusters, 2):
        pair = tuple(
            sorted(
                (
                    str(left.get("cluster_id") or ""),
                    str(right.get("cluster_id") or ""),
                )
            )
        )
        if pair in seen_relationship_pairs:
            continue
        shared_source_ids = sorted(
            set(str(value) for value in left.get("source_ids", []) or [])
            & set(str(value) for value in right.get("source_ids", []) or [])
        )
        left_roles = display_roles_by_cluster.get(str(left.get("cluster_id") or ""), {})
        right_roles = display_roles_by_cluster.get(
            str(right.get("cluster_id") or ""), {}
        )
        shared_source_ids = [
            source_id
            for source_id in shared_source_ids
            if (
                left_roles.get(source_id) == "core"
                and right_roles.get(source_id) in {"core", "bridge"}
            )
            or (
                right_roles.get(source_id) == "core"
                and left_roles.get(source_id) in {"core", "bridge"}
            )
            or (
                left_roles.get(source_id) == "bridge"
                and right_roles.get(source_id) == "bridge"
            )
        ]
        if not shared_source_ids:
            continue
        source_by_id = {
            str(row.get("source_id") or ""): row
            for row in [
                *(left.get("representative_sources", []) or []),
                *(right.get("representative_sources", []) or []),
            ]
            if isinstance(row, Mapping) and row.get("source_id")
        }
        shared_sources = [
            _obsidian_note_link(source_by_id[source_id])
            for source_id in shared_source_ids
            if source_id in source_by_id
        ]
        if not shared_sources:
            continue
        signature = _stable_hash(
            sorted(
                [str(left.get("cluster_id") or ""), str(right.get("cluster_id") or "")]
            )
            + shared_source_ids
        )
        if signature in seen_relationships:
            continue
        seen_relationships.add(signature)
        relationships.append(
            f"- {_cluster_wikilink(left)} and {_cluster_wikilink(right)} overlap through "
            + "; ".join(shared_sources)
            + ". The shared source connects the two questions, but each cluster retains a different analytical focus."
        )
    if relationships:
        sections.append("## How the clusters connect\n\n" + "\n".join(relationships))

    if gaps:
        sections.append(
            "## Collection-relative gaps\n\n"
            + "\n".join(
                f"- {_gap_wikilink(gap)} — "
                + (
                    "Collection gap lead: "
                    if str(gap.get("status") or "") == "collection_gap_lead"
                    else "Mapped collection gap: "
                )
                + _human_projection_text(
                    gap.get("gap_statement")
                    or gap.get("precise_missing_evidence")
                    or ""
                )
                for gap in gaps
            )
        )
    else:
        searched_count = len(report.get("internal_search_log", []) or [])
        if searched_count:
            gap_outcome = (
                f"No candidate survived adjudication after {searched_count} collection-wide internal search"
                f"{'es' if searched_count != 1 else ''}."
            )
        elif rejected_gaps:
            gap_outcome = (
                f"No candidate reached collection-wide adjudication. {len(rejected_gaps)} draft signal"
                f"{'s were' if len(rejected_gaps) != 1 else ' was'} retained in the audit registry but stopped at the "
                "lineage or specificity gate."
            )
        else:
            gap_outcome = (
                "No sufficiently specific collection-native candidate was generated."
            )
        sections.append(
            "## Collection-relative gaps\n\n"
            + gap_outcome
            + " That does not mean the wider literature has no gaps."
        )

    sections.append(
        "## Navigate\n\n"
        "- [[clusters/INDEX|Cluster Index]] — every admitted research conversation\n"
        "- [[gaps/INDEX|Gap Registry Index]] — visible collection-relative gaps and leads\n"
        "- [[02_source_memory/indexes/INDEX|Source Index]] — every generated source note"
    )
    return _markdown_with_frontmatter(
        {
            "type": "literature_map",
            "title": title,
            "map_id": map_id,
            "source_set_id": str(source_set.get("source_set_id") or ""),
            "scope": "collection_only",
            "status": "partial" if partial_cluster_ids else "complete",
        },
        "\n\n".join(sections),
    )


def _write_projection_yaml(path: Path, value: Any) -> None:
    """Avoid rewriting a generated projection when only timestamps changed."""

    existing = read_yaml(path, None)
    if existing is not None:
        existing_projection = yaml.safe_dump(
            _projection_without_volatile_timestamps(existing),
            sort_keys=False,
            allow_unicode=True,
        )
        requested_projection = yaml.safe_dump(
            _projection_without_volatile_timestamps(value),
            sort_keys=False,
            allow_unicode=True,
        )
        if existing_projection == requested_projection:
            return
        # Some live payloads contain string-like model values that compare
        # differently in memory but serialize to the same YAML scalar. Compare
        # the exact emitted form as a final guard for top-level run timestamps.
        existing_text = path.read_text(encoding="utf-8")
        requested_text = yaml.safe_dump(value, sort_keys=False, allow_unicode=True)
        timestamp_line = re.compile(
            r"^(created_at|updated_at):[^\n]*(?:\n|$)", re.MULTILINE
        )
        if timestamp_line.sub(
            r"\1: <volatile-timestamp>\n", existing_text
        ) == timestamp_line.sub(r"\1: <volatile-timestamp>\n", requested_text):
            return
    write_yaml(path, value)


def _preserve_existing_projection_fields(
    path: Path,
    value: Mapping[str, Any],
    fields: Sequence[str],
) -> dict[str, Any]:
    """Carry forward post-render lineage until its owning stage refreshes it."""

    projected = dict(value)
    existing = read_yaml(path, {}) or {}
    if not isinstance(existing, Mapping):
        return projected
    for field in fields:
        if field not in projected and field in existing:
            projected[field] = existing[field]
    return projected


def persist_literature_report(
    workspace: Path,
    report: Mapping[str, Any],
    *,
    source_set: Mapping[str, Any],
    run_id: str,
    question: str | None,
    map_id: str | None = None,
) -> tuple[dict[str, Any], list[Path]]:
    """Persist compatibility projections after all pure stages have completed."""
    # Generated literature artifacts are immutable for unchanged semantic
    # inputs. Route all YAML writes in this function through the projection
    # writer so a replay cannot churn hashes solely by refreshing timestamps.
    write_yaml = _write_projection_yaml
    root = workspace / "03_literature_synthesis"
    cluster_root = root / "clusters"
    gap_root = root / "gaps"
    gap_candidates_root = gap_root / "candidates"
    prior_root = root / "closest_prior_work"
    packet_root = root / "packets"
    map_id = map_id or stable_literature_map_id(source_set, question)
    map_root = root / "maps" / map_id
    canonical_cluster_root = map_root / "clusters"
    canonical_gap_root = map_root / "gaps"
    for directory in (
        cluster_root,
        gap_candidates_root,
        prior_root,
        packet_root,
        canonical_cluster_root,
        canonical_gap_root,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    generated_at = now_iso()
    clusters = list(report["cluster_registry"]["clusters"])
    gaps = list(report["gap_registry"]["gaps"])
    profile_by_source = {
        str(row.get("source_id") or ""): row
        for row in report.get("profiles", []) or []
        if row.get("source_id")
    }
    cluster_by_id = {str(row["cluster_id"]): row for row in clusters}
    gaps_by_cluster: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for gap in gaps:
        for cluster_id in gap.get("related_cluster_ids", []) or []:
            cluster_id = str(cluster_id)
            if cluster_id in cluster_by_id and gap not in gaps_by_cluster[cluster_id]:
                gaps_by_cluster[cluster_id].append(gap)
    rejected_gaps_by_cluster: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for gap in (
        _as_mapping(report.get("gap_registry")).get("rejected_candidates", []) or []
    ):
        if not isinstance(gap, Mapping) or "cluster_synthesis" not in set(
            str(value) for value in gap.get("proposal_origins", []) or []
        ):
            continue
        for cluster_id in gap.get("related_cluster_ids", []) or []:
            cluster_id = str(cluster_id)
            if cluster_id in cluster_by_id:
                rejected_gaps_by_cluster[cluster_id].append(dict(gap))
    matrix_by_cluster = {row["cluster_id"]: row for row in report["evidence_matrices"]}
    debate_by_cluster = {
        row["cluster_id"]: row for row in report["debate_registry"]["assessments"]
    }
    synthesis_by_cluster = {
        str(cluster_id): synthesis
        for cluster_id, synthesis in (report.get("cluster_syntheses", {}) or {}).items()
    }
    projection_publishable = all(
        _cluster_projection_is_publishable(
            _as_mapping(synthesis_by_cluster.get(str(cluster["cluster_id"])))
        )
        for cluster in clusters
    )
    if projection_publishable:
        for directory in (root, map_root):
            for stale_path in directory.glob("Literature Neighborhoods - *.md"):
                stale_path.unlink()
    prior_merge_payload = read_yaml(map_root / "gap_merge_ledger.yml", {}) or {}
    prior_merge_events = (
        list(prior_merge_payload.get("events", []) or [])
        if isinstance(prior_merge_payload, Mapping)
        else []
    )
    merge_events = sorted(
        {
            _stable_hash(event): dict(event)
            for event in [
                *prior_merge_events,
                *(report["gap_registry"].get("merge_ledger", []) or []),
            ]
            if isinstance(event, Mapping)
        }.values(),
        key=lambda row: (
            str(row.get("canonical_gap_id") or ""),
            str(row.get("merged_gap_id") or ""),
            str(row.get("event") or ""),
        ),
    )
    paths: list[Path] = []

    registry_path = root / "cluster_registry.yml"
    ledger_path = root / "cluster_ledger.yml"
    compatibility_clusters = cluster_root / "clusters.yml"
    cluster_updates = cluster_root / "cluster_updates.yml"
    write_yaml(
        registry_path, {"updated_at": generated_at, **dict(report["cluster_registry"])}
    )
    write_yaml(
        ledger_path,
        {"updated_at": generated_at, "events": report["cluster_registry"]["ledger"]},
    )
    write_yaml(
        compatibility_clusters,
        {
            "updated_at": generated_at,
            "minimum_independent_study_families": 2,
            "clusters": clusters,
            "unclustered_sources": report["cluster_registry"]["unclustered_sources"],
        },
    )
    write_yaml(
        cluster_updates,
        {
            "updated_at": generated_at,
            "updates": report["cluster_registry"]["rejected_proposals"],
            "component_actions": report["cluster_registry"].get(
                "component_actions", []
            ),
        },
    )
    paths.extend((registry_path, ledger_path, compatibility_clusters, cluster_updates))

    matrix_path = root / "evidence_matrices.yml"
    navigation_facets_path = root / "navigation_facets.yml"
    neighborhood_path = root / "topic_neighborhoods.yml"
    subject_tag_registry_path = root / "subject_tag_registry.yml"
    subject_tag_assignments_path = root / "subject_tag_assignments.yml"
    typed_relations_path = root / "typed_source_relations.yml"
    navigation_audit_path = root / "navigation_audit.yml"
    source_index_root = workspace / "02_source_memory" / "indexes"
    compatibility_subject_tag_registry_path = (
        source_index_root / "subject_tag_registry.yml"
    )
    compatibility_subject_tag_assignments_path = (
        source_index_root / "subject_tag_assignments.yml"
    )
    compatibility_typed_links_path = source_index_root / "typed_links.yml"
    compatibility_typed_note_links_path = source_index_root / "typed_note_links.yml"
    proposition_path = root / "propositions.yml"
    debate_path = root / "debate_registry.yml"
    synthesis_path = root / "cluster_syntheses.yml"
    study_lineage_path = root / "study_lineage_registry.yml"
    independence_path = root / "independence_assessments.yml"
    source_contributions_path = root / "cluster_source_contributions.yml"
    quantitative_path = root / "quantitative_comparisons.yml"
    locator_audit_path = root / "locator_audit.yml"
    coverage_register_path = root / "coverage_register.yml"
    tag_concept_registry_path = root / "tag_concept_registry.yml"
    write_yaml(
        matrix_path,
        {"updated_at": generated_at, "matrices": report["evidence_matrices"]},
    )
    navigation_facets_payload = {
        "updated_at": generated_at,
        "purpose": "machine_navigation_only",
        "navigation_facets": report.get("topic_neighborhoods", []),
    }
    write_yaml(navigation_facets_path, navigation_facets_payload)
    # Schema 1.0-1.8 readers may still look for the old filename and key.
    write_yaml(
        neighborhood_path,
        {
            "updated_at": generated_at,
            "purpose": "deprecated_compatibility_alias",
            "topic_neighborhoods": report.get("topic_neighborhoods", []),
        },
    )
    navigation = _as_mapping(report.get("navigation"))
    tag_registry_payload = {
        "updated_at": generated_at,
        "tag_reconciliation_version": navigation.get("tag_reconciliation_version", "1"),
        "graph_projection_hash": navigation.get("graph_projection_hash", ""),
        "subject_tags": navigation.get("subject_tags", []),
    }
    tag_assignments_payload = {
        "updated_at": generated_at,
        "tag_reconciliation_version": navigation.get("tag_reconciliation_version", "1"),
        "assignments": navigation.get("assignments", []),
    }
    typed_relations_payload = {
        "updated_at": generated_at,
        "navigation_relation_version": navigation.get(
            "navigation_relation_version", "1"
        ),
        "graph_projection_hash": navigation.get("graph_projection_hash", ""),
        "relation_counts": navigation.get("typed_relation_counts", {}),
        "relations": navigation.get("typed_relations", []),
        "links": navigation.get("typed_relations", []),
    }
    navigation_audit_payload = {
        "updated_at": generated_at,
        "graph_projection_hash": navigation.get("graph_projection_hash", ""),
        "candidate_count": navigation.get("candidate_count", 0),
        "rejected_generic_tag_count": navigation.get("rejected_generic_tag_count", 0),
        "unconfirmed_zotero_tag_count": navigation.get(
            "unconfirmed_zotero_tag_count", 0
        ),
        "singleton_facet_count": navigation.get("singleton_facet_count", 0),
        "candidates": navigation.get("candidates", []),
        "rejected_candidates": navigation.get("rejected_candidates", []),
        "singleton_facets": navigation.get("singleton_facets", []),
    }
    for path, payload in (
        (subject_tag_registry_path, tag_registry_payload),
        (compatibility_subject_tag_registry_path, tag_registry_payload),
        (subject_tag_assignments_path, tag_assignments_payload),
        (compatibility_subject_tag_assignments_path, tag_assignments_payload),
        (typed_relations_path, typed_relations_payload),
        (compatibility_typed_links_path, typed_relations_payload),
        (compatibility_typed_note_links_path, typed_relations_payload),
        (navigation_audit_path, navigation_audit_payload),
    ):
        write_yaml(path, payload)
    write_yaml(
        proposition_path,
        {"updated_at": generated_at, "propositions": report.get("propositions", [])},
    )
    write_yaml(
        debate_path, {"updated_at": generated_at, **dict(report["debate_registry"])}
    )
    write_yaml(
        synthesis_path, {"updated_at": generated_at, "syntheses": synthesis_by_cluster}
    )
    write_yaml(
        study_lineage_path,
        {
            "updated_at": generated_at,
            "version": STUDY_LINEAGE_VERSION,
            "study_lineages": report.get("study_lineages", []),
            "evidence_base_groups": report.get("evidence_base_groups", []),
        },
    )
    write_yaml(
        independence_path,
        {
            "updated_at": generated_at,
            "version": INDEPENDENCE_ALGORITHM_VERSION,
            "assessments": report.get("independence_assessments", []),
        },
    )
    write_yaml(
        source_contributions_path,
        {
            "updated_at": generated_at,
            "clusters": report.get("cluster_source_contributions", {}),
        },
    )
    write_yaml(
        quantitative_path,
        {
            "updated_at": generated_at,
            "comparisons": report.get("quantitative_comparisons", []),
        },
    )
    write_yaml(
        locator_audit_path,
        {"updated_at": generated_at, **dict(report.get("locator_audit", {}))},
    )
    write_yaml(
        coverage_register_path,
        dict(report.get("coverage_register", {})),
    )
    write_yaml(
        tag_concept_registry_path,
        {
            "updated_at": generated_at,
            "version": navigation.get("tag_reconciliation_version", "2"),
            "concepts": navigation.get("tag_concept_registry", []),
            "reconciliation_proposals": navigation.get(
                "tag_reconciliation_proposals", []
            ),
        },
    )
    paths.extend(
        (
            matrix_path,
            navigation_facets_path,
            neighborhood_path,
            subject_tag_registry_path,
            subject_tag_assignments_path,
            typed_relations_path,
            navigation_audit_path,
            compatibility_subject_tag_registry_path,
            compatibility_subject_tag_assignments_path,
            compatibility_typed_links_path,
            compatibility_typed_note_links_path,
            proposition_path,
            debate_path,
            synthesis_path,
            study_lineage_path,
            independence_path,
            source_contributions_path,
            quantitative_path,
            locator_audit_path,
            coverage_register_path,
            tag_concept_registry_path,
        )
    )

    gap_registry_path = root / "gap_registry.yml"
    compatibility_gaps = gap_root / "gaps.yml"
    compatibility_gap_index = (
        workspace / "02_source_memory" / "indexes" / "gap_candidates.yml"
    )
    gap_memory_path = root / "gap_memory.yml"
    gap_merge_ledger_path = root / "gap_merge_ledger.yml"
    search_path = root / "internal_search_log.yml"
    gap_status = (
        "complete_no_qualifying_gaps"
        if not clusters
        else (
            "mapped_collection_gaps"
            if any(row.get("promoted") for row in gaps)
            else ("gap_leads" if gaps else "complete_no_qualifying_gaps")
        )
    )
    gap_payload = {
        "updated_at": generated_at,
        "status": gap_status,
        "novelty_claimed": False,
        "allowed_rules": list(GAP_RULES),
        "gap_candidates": gaps,
    }
    write_yaml(
        gap_registry_path, {"updated_at": generated_at, **dict(report["gap_registry"])}
    )
    write_yaml(compatibility_gaps, gap_payload)
    write_yaml(compatibility_gap_index, gap_payload)
    write_yaml(
        gap_memory_path, {"updated_at": generated_at, "entries": report["gap_memory"]}
    )
    write_yaml(
        gap_merge_ledger_path,
        {"updated_at": generated_at, "events": merge_events},
    )
    write_yaml(
        search_path,
        {"updated_at": generated_at, "searches": report["internal_search_log"]},
    )
    paths.extend(
        (
            gap_registry_path,
            compatibility_gaps,
            compatibility_gap_index,
            gap_memory_path,
            gap_merge_ledger_path,
            search_path,
        )
    )

    if projection_publishable:
        _clear_generated_markdown(gap_candidates_root)
        _clear_generated_markdown(prior_root)
    gap_index = [
        "# Gap Index",
        "",
        "Only bounded, non-obvious collection gaps that survive internal search and feasibility checks appear here.",
        "",
    ]
    if not gaps:
        gap_index.extend(
            [
                "No candidate survived the specificity, non-obviousness, worth, and collection-wide falsification gates.",
                "",
                "This does not mean that the wider literature has no gaps; it means this frozen collection did not support a defensible visible gap.",
                "",
            ]
        )
    for gap in gaps:
        path = gap_candidates_root / f"{gap_note_stem(gap)}.md"
        gap_written = _write_markdown_with_quality_ratchet(
            path,
            _gap_markdown(
                gap, profile_by_source=profile_by_source, cluster_by_id=cluster_by_id
            ),
            publishable=projection_publishable,
        )
        gap_index.append(
            f"- {_gap_wikilink(gap)} — {str(gap['rule']).replace('_', ' ')}; "
            f"{str(gap['status']).replace('_', ' ')}; rank {gap['rank']}"
        )
        if gap_written or path.is_file():
            paths.append(path)
        prior_path = (
            prior_root
            / f"Closest Prior - {gap_note_stem(gap).removeprefix('Gap - ')}.md"
        )
        prior_lines = [
            f"# Closest Prior: {gap_display_title(gap).removeprefix('Gap: ')}",
            "",
            f"Gap ID: `{gap['gap_id']}`",
            "",
        ]
        prior_lines.extend(
            f"- `{row['prior_id']}` — {row['title']} — confidence {row['confidence']} — {row['overlap_explanation']}"
            for row in gap.get("closest_prior_work", []) or []
        )
        if not gap.get("closest_prior_work"):
            prior_lines.append(
                "- No semantic overlap in the mapped analytical profiles."
            )
        prior_written = _write_markdown_with_quality_ratchet(
            prior_path,
            "\n".join(prior_lines) + "\n",
            publishable=projection_publishable,
        )
        if prior_written or prior_path.is_file():
            paths.append(prior_path)
    gap_index_path = gap_root / "INDEX.md"
    _write_markdown_with_quality_ratchet(
        gap_index_path,
        _markdown_with_frontmatter(
            {"type": "literature_gap_index", "tags": []},
            "\n".join(gap_index),
        ),
        publishable=projection_publishable,
    )
    paths.append(gap_index_path)

    current_cluster_filenames = {
        f"{cluster_note_stem(cluster)}.md" for cluster in clusters
    }
    cluster_index = [
        "# Cluster Index",
        "",
        "Each cluster is a bounded subliterature. Open a note for the full findings, disagreements, limits, and sources.",
        "",
    ]
    for cluster in clusters:
        path = cluster_root / f"{cluster_note_stem(cluster)}.md"
        synthesis = _as_mapping(synthesis_by_cluster.get(str(cluster["cluster_id"])))
        cluster_publishable = _cluster_projection_is_publishable(synthesis)
        if cluster_publishable:
            atomic_write_text(
                path,
                _cluster_markdown(
                    cluster,
                    matrix_by_cluster.get(cluster["cluster_id"]),
                    debate_by_cluster.get(cluster["cluster_id"]),
                    gaps_by_cluster.get(str(cluster["cluster_id"]), []),
                    rejected_gap_candidates=rejected_gaps_by_cluster.get(
                        str(cluster["cluster_id"]), []
                    ),
                    synthesis=synthesis,
                    profile_by_source=profile_by_source,
                    cluster_by_id={
                        str(row.get("cluster_id") or ""): row for row in clusters
                    },
                ),
            )
        question_text = _cluster_display_question(cluster, synthesis)
        verdict_text = _cluster_answer_excerpt(
            synthesis, cluster=cluster, max_threads=2, character_limit=550
        )
        display_roles = _cluster_display_source_roles(cluster, synthesis)
        display_core_count = sum(role == "core" for role in display_roles.values())
        cluster_index.extend(
            [
                f"## {_cluster_wikilink(cluster)}",
                *([f"**Question:** {question_text}"] if question_text else []),
                f"**Evidence base:** {_cluster_researcher_status(cluster)}",
                f"**Current answer:** {verdict_text or 'Narrative synthesis is still pending.'}",
                f"*{display_core_count} core source{'s' if display_core_count != 1 else ''}.*",
                "",
            ]
        )
        if path.is_file():
            paths.append(path)
    if projection_publishable:
        _prune_stale_generated_markdown(
            cluster_root, keep_names=sorted(current_cluster_filenames)
        )
    cluster_index_path = cluster_root / "INDEX.md"
    _write_markdown_with_quality_ratchet(
        cluster_index_path,
        _markdown_with_frontmatter(
            {"type": "literature_cluster_index", "tags": []},
            "\n".join(cluster_index),
        ),
        publishable=projection_publishable,
    )
    paths.append(cluster_index_path)

    packet_id = f"literature-packet-{slugify(map_id)}"
    packet = {
        **dict(report["packet"]),
        "packet_id": packet_id,
        "map_id": map_id,
        "run_id": run_id,
        "source_set_id": source_set.get("source_set_id", ""),
        "question": question or "",
        "graph_projection_hash": str(
            report.get("manifest", {}).get("graph_projection_hash") or ""
        ),
        "dependency_hash": _stable_hash(
            {
                "algorithm_version": LITERATURE_ALGORITHM_VERSION,
                "source_set_dependency_hash": source_set.get("dependency_hash", ""),
                "cluster_revisions": [row["revision_hash"] for row in clusters],
                "gap_memory": report["gap_memory"],
            }
        ),
        "created_at": generated_at,
    }
    packet_path = packet_root / f"{packet_id}.yml"
    packet = _preserve_existing_projection_fields(
        packet_path,
        packet,
        ("profile_packet_count", "profile_packet_paths"),
    )
    write_yaml(packet_path, packet)
    paths.append(packet_path)

    map_note_stem = literature_map_note_stem(source_set, map_id)
    map_markdown = _literature_map_markdown(report, source_set, map_id=map_id)
    literature_map_path = root / f"{map_note_stem}.md"
    index_path = root / "INDEX.md"
    manifest = report["manifest"]
    _write_markdown_with_quality_ratchet(
        literature_map_path,
        map_markdown,
        publishable=projection_publishable,
    )
    _write_markdown_with_quality_ratchet(
        index_path,
        _markdown_with_frontmatter(
            {
                "type": "literature_map_pointer",
                "primary_map": f"[[{map_note_stem}]]",
                "tags": [],
            },
            f"# Literature Map\n\nOpen [[{map_note_stem}|the collection literature map]].",
        ),
        publishable=projection_publishable,
    )
    paths.extend((literature_map_path, index_path))

    canonical_registry_path = map_root / "cluster_registry.yml"
    canonical_ledger_path = map_root / "cluster_ledger.yml"
    canonical_matrix_path = map_root / "evidence_matrices.yml"
    canonical_navigation_facets_path = map_root / "navigation_facets.yml"
    canonical_neighborhood_path = map_root / "topic_neighborhoods.yml"
    canonical_subject_tag_registry_path = map_root / "subject_tag_registry.yml"
    canonical_subject_tag_assignments_path = map_root / "subject_tag_assignments.yml"
    canonical_typed_relations_path = map_root / "typed_source_relations.yml"
    canonical_navigation_audit_path = map_root / "navigation_audit.yml"
    canonical_proposition_path = map_root / "propositions.yml"
    canonical_debate_path = map_root / "debate_registry.yml"
    canonical_synthesis_path = map_root / "cluster_syntheses.yml"
    canonical_study_lineage_path = map_root / "study_lineage_registry.yml"
    canonical_independence_path = map_root / "independence_assessments.yml"
    canonical_source_contributions_path = map_root / "cluster_source_contributions.yml"
    canonical_quantitative_path = map_root / "quantitative_comparisons.yml"
    canonical_locator_audit_path = map_root / "locator_audit.yml"
    canonical_coverage_register_path = map_root / "coverage_register.yml"
    canonical_tag_concept_registry_path = map_root / "tag_concept_registry.yml"
    canonical_gap_registry_path = map_root / "gap_registry.yml"
    canonical_gap_memory_path = map_root / "gap_memory.yml"
    canonical_gap_merge_ledger_path = map_root / "gap_merge_ledger.yml"
    canonical_search_path = map_root / "internal_search_log.yml"
    canonical_packet_path = map_root / "packet.yml"
    write_yaml(
        canonical_registry_path,
        {"updated_at": generated_at, **dict(report["cluster_registry"])},
    )
    write_yaml(
        canonical_ledger_path,
        {"updated_at": generated_at, "events": report["cluster_registry"]["ledger"]},
    )
    write_yaml(
        canonical_matrix_path,
        {"updated_at": generated_at, "matrices": report["evidence_matrices"]},
    )
    write_yaml(canonical_navigation_facets_path, navigation_facets_payload)
    write_yaml(
        canonical_neighborhood_path,
        {
            "updated_at": generated_at,
            "purpose": "deprecated_compatibility_alias",
            "topic_neighborhoods": report.get("topic_neighborhoods", []),
        },
    )
    write_yaml(canonical_subject_tag_registry_path, tag_registry_payload)
    write_yaml(canonical_subject_tag_assignments_path, tag_assignments_payload)
    write_yaml(canonical_typed_relations_path, typed_relations_payload)
    write_yaml(canonical_navigation_audit_path, navigation_audit_payload)
    write_yaml(
        canonical_proposition_path,
        {"updated_at": generated_at, "propositions": report.get("propositions", [])},
    )
    write_yaml(
        canonical_debate_path,
        {"updated_at": generated_at, **dict(report["debate_registry"])},
    )
    write_yaml(
        canonical_synthesis_path,
        {"updated_at": generated_at, "syntheses": synthesis_by_cluster},
    )
    write_yaml(
        canonical_study_lineage_path,
        {
            "updated_at": generated_at,
            "version": STUDY_LINEAGE_VERSION,
            "study_lineages": report.get("study_lineages", []),
            "evidence_base_groups": report.get("evidence_base_groups", []),
        },
    )
    write_yaml(
        canonical_independence_path,
        {
            "updated_at": generated_at,
            "version": INDEPENDENCE_ALGORITHM_VERSION,
            "assessments": report.get("independence_assessments", []),
        },
    )
    write_yaml(
        canonical_source_contributions_path,
        {
            "updated_at": generated_at,
            "clusters": report.get("cluster_source_contributions", {}),
        },
    )
    write_yaml(
        canonical_quantitative_path,
        {
            "updated_at": generated_at,
            "comparisons": report.get("quantitative_comparisons", []),
        },
    )
    write_yaml(
        canonical_locator_audit_path,
        {"updated_at": generated_at, **dict(report.get("locator_audit", {}))},
    )
    write_yaml(
        canonical_coverage_register_path,
        dict(report.get("coverage_register", {})),
    )
    write_yaml(
        canonical_tag_concept_registry_path,
        {
            "updated_at": generated_at,
            "version": navigation.get("tag_reconciliation_version", "2"),
            "concepts": navigation.get("tag_concept_registry", []),
            "reconciliation_proposals": navigation.get(
                "tag_reconciliation_proposals", []
            ),
        },
    )
    write_yaml(
        canonical_gap_registry_path,
        {"updated_at": generated_at, **dict(report["gap_registry"])},
    )
    write_yaml(
        canonical_gap_memory_path,
        {"updated_at": generated_at, "entries": report["gap_memory"]},
    )
    write_yaml(
        canonical_gap_merge_ledger_path,
        {"updated_at": generated_at, "events": merge_events},
    )
    write_yaml(
        canonical_search_path,
        {"updated_at": generated_at, "searches": report["internal_search_log"]},
    )
    packet = _preserve_existing_projection_fields(
        canonical_packet_path,
        packet,
        ("profile_packet_count", "profile_packet_paths"),
    )
    write_yaml(canonical_packet_path, packet)
    paths.extend(
        (
            canonical_registry_path,
            canonical_ledger_path,
            canonical_matrix_path,
            canonical_navigation_facets_path,
            canonical_neighborhood_path,
            canonical_subject_tag_registry_path,
            canonical_subject_tag_assignments_path,
            canonical_typed_relations_path,
            canonical_navigation_audit_path,
            canonical_proposition_path,
            canonical_debate_path,
            canonical_synthesis_path,
            canonical_study_lineage_path,
            canonical_independence_path,
            canonical_source_contributions_path,
            canonical_quantitative_path,
            canonical_locator_audit_path,
            canonical_coverage_register_path,
            canonical_tag_concept_registry_path,
            canonical_gap_registry_path,
            canonical_gap_memory_path,
            canonical_gap_merge_ledger_path,
            canonical_search_path,
            canonical_packet_path,
        )
    )

    canonical_gap_index = [
        "# Gap Index",
        "",
        "Only bounded, non-obvious collection gaps that survive internal search and feasibility checks appear here.",
        "",
    ]
    if not gaps:
        canonical_gap_index.extend(
            [
                "No candidate survived the specificity, non-obviousness, worth, and collection-wide falsification gates.",
                "",
                "This does not mean that the wider literature has no gaps; it means this frozen collection did not support a defensible visible gap.",
                "",
            ]
        )
    if projection_publishable:
        _clear_generated_markdown(canonical_gap_root)
    for gap in gaps:
        canonical_gap_path = canonical_gap_root / f"{gap_note_stem(gap)}.md"
        _write_markdown_with_quality_ratchet(
            canonical_gap_path,
            _gap_markdown(
                gap, profile_by_source=profile_by_source, cluster_by_id=cluster_by_id
            ),
            publishable=projection_publishable,
        )
        canonical_gap_index.append(
            f"- {_gap_wikilink(gap)} — {str(gap['rule']).replace('_', ' ')}; "
            f"{str(gap['status']).replace('_', ' ')}; rank {gap['rank']}"
        )
        if canonical_gap_path.is_file():
            paths.append(canonical_gap_path)
    canonical_gap_index_path = canonical_gap_root / "INDEX.md"
    _write_markdown_with_quality_ratchet(
        canonical_gap_index_path,
        _markdown_with_frontmatter(
            {"type": "literature_gap_index", "tags": []},
            "\n".join(canonical_gap_index),
        ),
        publishable=projection_publishable,
    )
    paths.append(canonical_gap_index_path)

    canonical_cluster_index = [
        "# Cluster Index",
        "",
        "Each cluster is a bounded subliterature. Open a note for the full findings, disagreements, limits, and sources.",
        "",
    ]
    canonical_cluster_filenames = {
        f"{cluster_note_stem(cluster)}.md" for cluster in clusters
    }
    for cluster in clusters:
        canonical_cluster_path = (
            canonical_cluster_root / f"{cluster_note_stem(cluster)}.md"
        )
        synthesis = _as_mapping(synthesis_by_cluster.get(str(cluster["cluster_id"])))
        if _cluster_projection_is_publishable(synthesis):
            atomic_write_text(
                canonical_cluster_path,
                _cluster_markdown(
                    cluster,
                    matrix_by_cluster.get(cluster["cluster_id"]),
                    debate_by_cluster.get(cluster["cluster_id"]),
                    gaps_by_cluster.get(str(cluster["cluster_id"]), []),
                    rejected_gap_candidates=rejected_gaps_by_cluster.get(
                        str(cluster["cluster_id"]), []
                    ),
                    synthesis=synthesis,
                    profile_by_source=profile_by_source,
                    cluster_by_id={
                        str(row.get("cluster_id") or ""): row for row in clusters
                    },
                ),
            )
        question_text = _cluster_display_question(cluster, synthesis)
        verdict_text = _cluster_answer_excerpt(
            synthesis, cluster=cluster, max_threads=2, character_limit=550
        )
        display_roles = _cluster_display_source_roles(cluster, synthesis)
        display_core_count = sum(role == "core" for role in display_roles.values())
        canonical_cluster_index.extend(
            [
                f"## {_cluster_wikilink(cluster)}",
                *([f"**Question:** {question_text}"] if question_text else []),
                f"**Evidence base:** {_cluster_researcher_status(cluster)}",
                f"**Current answer:** {verdict_text or 'Narrative synthesis is still pending.'}",
                f"*{display_core_count} core source{'s' if display_core_count != 1 else ''}.*",
                "",
            ]
        )
        if canonical_cluster_path.is_file():
            paths.append(canonical_cluster_path)
    if projection_publishable:
        _prune_stale_generated_markdown(
            canonical_cluster_root,
            keep_names=sorted(canonical_cluster_filenames),
        )
    canonical_cluster_index_path = canonical_cluster_root / "INDEX.md"
    _write_markdown_with_quality_ratchet(
        canonical_cluster_index_path,
        _markdown_with_frontmatter(
            {"type": "literature_cluster_index", "tags": []},
            "\n".join(canonical_cluster_index),
        ),
        publishable=projection_publishable,
    )
    paths.append(canonical_cluster_index_path)

    canonical_literature_map_path = map_root / f"{map_note_stem}.md"
    canonical_index_path = map_root / "INDEX.md"
    _write_markdown_with_quality_ratchet(
        canonical_literature_map_path,
        map_markdown,
        publishable=projection_publishable,
    )
    _write_markdown_with_quality_ratchet(
        canonical_index_path,
        _markdown_with_frontmatter(
            {
                "type": "literature_map_pointer",
                "primary_map": f"[[{map_note_stem}]]",
                "tags": [],
            },
            f"# Literature Map\n\nOpen [[{map_note_stem}|the collection literature map]].",
        ),
        publishable=projection_publishable,
    )
    paths.extend(
        (
            canonical_literature_map_path,
            canonical_index_path,
        )
    )

    canonical_manifest_path = map_root / "manifest.yml"
    canonical_artifacts = {
        "manifest": str(canonical_manifest_path),
        "cluster_registry": str(canonical_registry_path),
        "cluster_ledger": str(canonical_ledger_path),
        "evidence_matrices": str(canonical_matrix_path),
        "navigation_facets": str(canonical_navigation_facets_path),
        "topic_neighborhoods": str(canonical_neighborhood_path),
        "subject_tag_registry": str(canonical_subject_tag_registry_path),
        "subject_tag_assignments": str(canonical_subject_tag_assignments_path),
        "typed_source_relations": str(canonical_typed_relations_path),
        "navigation_audit": str(canonical_navigation_audit_path),
        "propositions": str(canonical_proposition_path),
        "debate_registry": str(canonical_debate_path),
        "cluster_syntheses": str(canonical_synthesis_path),
        "study_lineage_registry": str(canonical_study_lineage_path),
        "independence_assessments": str(canonical_independence_path),
        "cluster_source_contributions": str(canonical_source_contributions_path),
        "quantitative_comparisons": str(canonical_quantitative_path),
        "locator_audit": str(canonical_locator_audit_path),
        "coverage_register": str(canonical_coverage_register_path),
        "tag_concept_registry": str(canonical_tag_concept_registry_path),
        "gap_registry": str(canonical_gap_registry_path),
        "gap_memory": str(canonical_gap_memory_path),
        "gap_merge_ledger": str(canonical_gap_merge_ledger_path),
        "internal_search_log": str(canonical_search_path),
        "packet": str(canonical_packet_path),
        "literature_map_markdown": str(canonical_literature_map_path),
        "index": str(canonical_index_path),
        "cluster_index": str(canonical_cluster_index_path),
        "gap_index": str(canonical_gap_index_path),
    }
    manifest_lineage_fields = (
        "note_projection_hashes",
        "semantic_note_hashes",
        "profile_dependency_hashes",
        "source_set_id",
        "source_set_dependency_hash",
        "provider",
        "model",
        "literature_policy",
        "algorithm_versions",
        "validation_mode",
    )
    canonical_manifest_payload = _preserve_existing_projection_fields(
        canonical_manifest_path,
        {
            "updated_at": generated_at,
            "map_id": map_id,
            "run_id": run_id,
            "source_set_id": source_set.get("source_set_id", ""),
            "source_set_dependency_hash": source_set.get("dependency_hash", ""),
            "engine_version": "0.10.0",
            "artifact_schema_version": "1.9",
            **dict(manifest),
            "artifacts": canonical_artifacts,
        },
        manifest_lineage_fields,
    )
    write_yaml(canonical_manifest_path, canonical_manifest_payload)
    paths.append(canonical_manifest_path)

    manifest_path = root / "manifest.yml"
    artifact_names = {
        "manifest": str(manifest_path),
        "cluster_registry": str(registry_path),
        "cluster_ledger": str(ledger_path),
        "evidence_matrices": str(matrix_path),
        "navigation_facets": str(navigation_facets_path),
        "topic_neighborhoods": str(neighborhood_path),
        "subject_tag_registry": str(subject_tag_registry_path),
        "subject_tag_assignments": str(subject_tag_assignments_path),
        "typed_source_relations": str(typed_relations_path),
        "navigation_audit": str(navigation_audit_path),
        "propositions": str(proposition_path),
        "debate_registry": str(debate_path),
        "cluster_syntheses": str(synthesis_path),
        "study_lineage_registry": str(study_lineage_path),
        "independence_assessments": str(independence_path),
        "cluster_source_contributions": str(source_contributions_path),
        "quantitative_comparisons": str(quantitative_path),
        "locator_audit": str(locator_audit_path),
        "coverage_register": str(coverage_register_path),
        "tag_concept_registry": str(tag_concept_registry_path),
        "gap_registry": str(gap_registry_path),
        "gap_memory": str(gap_memory_path),
        "gap_merge_ledger": str(gap_merge_ledger_path),
        "internal_search_log": str(search_path),
        "packet": str(packet_path),
        "literature_map_markdown": str(literature_map_path),
        "index": str(index_path),
        "canonical_map": str(canonical_manifest_path),
    }
    manifest_payload = _preserve_existing_projection_fields(
        manifest_path,
        {
            "updated_at": generated_at,
            "map_id": map_id,
            "engine_version": "0.10.0",
            "artifact_schema_version": "1.9",
            **dict(manifest),
            "artifacts": artifact_names,
        },
        manifest_lineage_fields,
    )
    write_yaml(manifest_path, manifest_payload)
    paths.insert(0, manifest_path)
    packet = {
        **packet,
        "path": str(canonical_packet_path),
        "compatibility_path": str(packet_path),
        "map_path": str(map_root),
        "literature_map_markdown": str(canonical_literature_map_path),
        "compatibility_literature_map_markdown": str(literature_map_path),
    }
    return packet, paths


def build_literature_map(
    workspace: Path,
    *,
    source_set: Mapping[str, Any],
    notes: Sequence[Mapping[str, Any]],
    question: str | None,
    run_id: str,
    profiles: Sequence[Any] | None = None,
    request: Any = None,
    policy: Any = None,
    reasoner: Any = None,
    stage_callback: Any = None,
    navigation_policy: Any = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[Path]]:
    """Compatibility entry point for current pipeline callers."""
    request_values = _as_mapping(request) if request is not None else {}
    effective_policy = policy or request_values.get("literature_policy")
    effective_question = (
        question if question is not None else request_values.get("question")
    )
    effective_run_id = run_id or str(
        request_values.get("run_id") or request_values.get("map_id") or "literature-map"
    )
    map_id = stable_literature_map_id(source_set, effective_question)
    previous_registry = _load_map_cluster_registry(workspace, map_id)
    reasoner_calls = (
        _CheckpointedReasonerCalls(
            workspace,
            effective_run_id,
            reasoner,
            request,
            stage_callback=stage_callback,
        )
        if reasoner is not None
        and not isinstance(reasoner, Mapping)
        and request is not None
        else None
    )
    navigation_source_notes = _source_notes_with_custody_relations(workspace, notes)
    report = build_literature_report(
        profiles if profiles is not None else notes,
        previous_registry=previous_registry,
        policy=effective_policy,
        question=effective_question,
        reasoner=reasoner,
        request=request,
        stage_callback=stage_callback,
        reasoner_call=reasoner_calls,
        source_notes=navigation_source_notes,
        navigation_policy=navigation_policy,
        source_set=source_set,
    )
    if reasoner_calls is not None:
        report["manifest"].update(
            {
                "synthesis_call_count": reasoner_calls.provider_calls,
                "synthesis_checkpoint_hit_count": reasoner_calls.checkpoint_hits,
                "synthesis_failure_count": reasoner_calls.failures,
            }
        )
        report["packet"].update(
            {
                "synthesis_call_count": reasoner_calls.provider_calls,
                "synthesis_checkpoint_hit_count": reasoner_calls.checkpoint_hits,
                "synthesis_failure_count": reasoner_calls.failures,
            }
        )
    packet, paths = persist_literature_report(
        workspace,
        report,
        source_set=source_set,
        run_id=effective_run_id,
        question=effective_question,
        map_id=map_id,
    )
    clusters = report["cluster_registry"]["clusters"]
    gaps = report["gap_registry"]["gaps"]
    promoted_clusters = [row for row in clusters if row.get("promoted")]
    partial_cluster_ids = list(packet.get("partial_cluster_ids", []) or [])
    cluster_map = {
        "status": (
            "partial"
            if partial_cluster_ids
            else (
                "built"
                if promoted_clusters
                else (
                    "cluster_candidates"
                    if clusters
                    else "complete_no_analytical_clusters"
                )
            )
        ),
        "automation_status": (
            "promoted"
            if promoted_clusters
            else ("candidate" if clusters else "not_applicable")
        ),
        "clusters": clusters,
        "relations": report["relations"],
        "topic_neighborhoods": report.get("topic_neighborhoods", []),
        "navigation": report.get("navigation", {}),
        "propositions": report.get("propositions", []),
        "topic_neighborhood_count": report["manifest"].get(
            "topic_neighborhood_count", 0
        ),
        "proposition_count": report["manifest"].get("proposition_count", 0),
        "rejected_proposals": report["cluster_registry"]["rejected_proposals"],
        "component_actions": report["cluster_registry"].get("component_actions", []),
        "unclustered_sources": report["cluster_registry"]["unclustered_sources"],
        "cluster_syntheses": report["cluster_syntheses"],
        "synthesized_cluster_count": report["manifest"]["synthesized_cluster_count"],
        "partial_cluster_synthesis_count": report["manifest"][
            "partial_cluster_synthesis_count"
        ],
        "partial_cluster_ids": partial_cluster_ids,
        "partial_reason": str(packet.get("partial_reason") or ""),
        "evidence_base_group_count": report["manifest"].get(
            "evidence_base_group_count", 0
        ),
        "cluster_source_contribution_count": report["manifest"].get(
            "cluster_source_contribution_count", 0
        ),
        "evidence_concentrated_cluster_count": report["manifest"].get(
            "evidence_concentrated_cluster_count", 0
        ),
        "strict_consensus_established_count": report["manifest"].get(
            "strict_consensus_established_count", 0
        ),
        "strict_consensus_not_established_count": report["manifest"].get(
            "strict_consensus_not_established_count", 0
        ),
        "strict_contradiction_established_count": report["manifest"].get(
            "strict_contradiction_established_count", 0
        ),
        "strict_contradiction_not_established_count": report["manifest"].get(
            "strict_contradiction_not_established_count", 0
        ),
        "quantitative_comparison_count": report["manifest"].get(
            "quantitative_comparison_count", 0
        ),
        "rejected_quantitative_comparison_count": report["manifest"].get(
            "rejected_quantitative_comparison_count", 0
        ),
        "rejected_generated_locator_count": report["manifest"].get(
            "rejected_generated_locator_count", 0
        ),
        "coverage_inventory_count": report["manifest"].get(
            "coverage_inventory_count", 0
        ),
        "coverage_exhausted_count": report["manifest"].get(
            "coverage_exhausted_count", 0
        ),
        "coverage_accounting_valid": report["manifest"].get(
            "coverage_accounting_valid", False
        ),
        "minimum_analytical_notes": 2,
        "path": str(
            workspace / "03_literature_synthesis" / "clusters" / "clusters.yml"
        ),
        "registry_path": str(
            workspace / "03_literature_synthesis" / "cluster_registry.yml"
        ),
    }
    gap_status = (
        "complete_no_qualifying_gaps"
        if not clusters
        else (
            "mapped_collection_gaps"
            if any(row["promoted"] for row in gaps)
            else ("gap_leads" if gaps else "complete_no_qualifying_gaps")
        )
    )
    gap_map = {
        "status": gap_status,
        "gap_candidates": gaps,
        "rejected_candidates": report["gap_registry"].get("rejected_candidates", []),
        "rejected_underspecified_gap_count": report["manifest"][
            "rejected_underspecified_gap_count"
        ],
        "rejected_gap_quality_count": report["manifest"]["rejected_gap_quality_count"],
        "merged_gap_count": report["manifest"]["merged_gap_count"],
        "strong_gap_established_count": report["manifest"].get(
            "strong_gap_established_count", 0
        ),
        "strong_gap_not_established_count": report["manifest"].get(
            "strong_gap_not_established_count", 0
        ),
        "gap_merge_ledger": report["gap_registry"].get("merge_ledger", []),
        "gap_merge_ledger_path": str(
            workspace / "03_literature_synthesis" / "gap_merge_ledger.yml"
        ),
        "novelty_claimed": False,
        "path": str(workspace / "03_literature_synthesis" / "gaps" / "gaps.yml"),
        "registry_path": str(
            workspace / "03_literature_synthesis" / "gap_registry.yml"
        ),
    }
    return cluster_map, gap_map, packet, paths


def run_literature_map(
    request: Any,
    *,
    profiles: Sequence[Any],
    source_set: Mapping[str, Any],
    reasoner: Any = None,
    stage_callback: Any = None,
) -> Any:
    """Public entry point for proposition-anchored mapping over existing profiles."""
    from .models import LiteratureMapReport

    values = _as_mapping(request)
    workspace = Path(values.get("workspace") or ".").expanduser()
    question = values.get("question") or None
    policy = values.get("literature_policy")
    run_id = str(values.get("run_id") or "")
    map_id = stable_literature_map_id(source_set, question)
    if not bool(_policy_value(policy, "synthesis_enabled", True)):
        return LiteratureMapReport(
            status="disabled",
            map_id=map_id,
            run_id=run_id,
            source_set_id=str(
                source_set.get("source_set_id") or values.get("source_set_id") or ""
            ),
            stage="policy_gate",
            counts={"profile_count": len(profiles)},
            partial_reason="synthesis_disabled",
        )
    if bool(_policy_value(policy, "require_question", False)) and not question:
        return LiteratureMapReport(
            status="blocked",
            map_id=map_id,
            run_id=run_id,
            source_set_id=str(
                source_set.get("source_set_id") or values.get("source_set_id") or ""
            ),
            stage="policy_gate",
            counts={"profile_count": len(profiles)},
            partial_reason="question_required",
        )
    if (
        reasoner is not None
        and bool(getattr(reasoner, "is_cloud", False))
        and not bool(values.get("allow_cloud", False))
    ):
        return LiteratureMapReport(
            status="blocked",
            map_id=map_id,
            run_id=run_id,
            source_set_id=str(
                source_set.get("source_set_id") or values.get("source_set_id") or ""
            ),
            stage="policy_gate",
            counts={"profile_count": len(profiles)},
            partial_reason="cloud_reasoner_not_allowed",
        )

    previous_registry = _load_map_cluster_registry(workspace, map_id)
    reasoner_calls = (
        _CheckpointedReasonerCalls(
            workspace,
            run_id or "literature-map",
            reasoner,
            request,
            stage_callback=stage_callback,
        )
        if reasoner is not None and not isinstance(reasoner, Mapping)
        else None
    )
    try:
        report = build_literature_report(
            profiles,
            previous_registry=previous_registry
            if isinstance(previous_registry, Mapping)
            else {},
            policy=policy,
            question=question,
            reasoner=reasoner,
            request=request,
            stage_callback=stage_callback,
            reasoner_call=reasoner_calls,
            source_set=source_set,
        )
    except LiteratureSynthesisPartialError as exc:
        return LiteratureMapReport(
            status="partial",
            map_id=map_id,
            run_id=run_id,
            source_set_id=str(
                source_set.get("source_set_id") or values.get("source_set_id") or ""
            ),
            stage="literature_synthesis",
            counts={
                "profile_count": len(profiles),
                "synthesis_call_count": reasoner_calls.provider_calls
                if reasoner_calls
                else 0,
                "synthesis_checkpoint_hit_count": reasoner_calls.checkpoint_hits
                if reasoner_calls
                else 0,
                "synthesized_cluster_count": reasoner_calls.synthesized_clusters
                if reasoner_calls
                else 0,
            },
            partial_reason=str(exc),
        )
    if reasoner_calls is not None:
        report["manifest"].update(
            {
                "synthesis_call_count": reasoner_calls.provider_calls,
                "synthesis_checkpoint_hit_count": reasoner_calls.checkpoint_hits,
                "synthesis_failure_count": reasoner_calls.failures,
            }
        )
    _, paths = persist_literature_report(
        workspace,
        report,
        source_set=source_set,
        run_id=run_id,
        question=question,
        map_id=map_id,
    )
    map_root = workspace / "03_literature_synthesis" / "maps" / map_id
    canonical_manifest = read_yaml(map_root / "manifest.yml", {}) or {}
    artifacts = (
        canonical_manifest.get("artifacts", {})
        if isinstance(canonical_manifest, Mapping)
        else {}
    )
    artifact_paths = {
        str(key): Path(str(value)).relative_to(workspace)
        if Path(str(value)).is_absolute()
        else Path(str(value))
        for key, value in artifacts.items()
    }
    return LiteratureMapReport(
        status="completed",
        map_id=map_id,
        run_id=run_id,
        source_set_id=str(
            source_set.get("source_set_id") or values.get("source_set_id") or ""
        ),
        stage="completed",
        counts={**dict(report["manifest"]), "written_artifact_count": len(paths)},
        artifact_paths=artifact_paths,
    )


# Small aliases keep stage discovery straightforward for callers using generic names.
map_relations = map_profile_relations
cluster_profiles = map_overlapping_clusters
