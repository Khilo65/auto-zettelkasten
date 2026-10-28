"""Evidence-bound label projection onto an unchanged saved semantic map.

This module performs no filesystem writes or model calls. Callers must compare
the saved-map and effective-document snapshots again before publishing a result.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import PurePosixPath
import unicodedata

from .community_labels import propose_community_labels
from .mapping_text import prepare_embedding_spans


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def bind_naming_corpus(mapping, snapshot):
    """Bind effective Markdown to canonical saved identities, never inferred IDs."""
    if snapshot.get("truncated"):
        raise ValueError("Naming requires a complete document snapshot")
    documents = {}
    for row in snapshot["documents"]:
        if row["path"] in documents:
            raise ValueError("Duplicate document path")
        documents[row["path"]] = row
    corpus, identities, paths = [], set(), set()
    for source in mapping["sources"]:
        sid, path = source["source_id"], source["note_path"]
        if (sid in identities or path in paths or not isinstance(path, str)
                or PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts):
            raise ValueError("Ambiguous or invalid saved source binding")
        document = documents.get(path)
        if not document or document.get("frontmatter", {}).get("note_id") != source["note_id"]:
            raise ValueError("Saved source does not match the effective note identity")
        declared = document.get("frontmatter", {}).get("source_id")
        if declared is not None and declared != sid:
            raise ValueError("Explicit note source identity conflicts with the saved source")
        prepared = prepare_embedding_spans(document["text"])
        text = "".join(row["text"] for row in prepared["spans"])
        if not text.strip():
            raise ValueError("Source has no substantive naming evidence")
        title = document.get("frontmatter", {}).get("title")
        title = title if isinstance(title, str) and title.strip() else PurePosixPath(path).stem
        corpus.append({"source_id": sid, "title": title,
                       "text": text, "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
                       "note_path": path, "revision": document["revision"]})
        identities.add(sid)
        paths.add(path)
    return sorted(corpus, key=lambda row: row["source_id"])


def naming_partitions(mapping):
    """Return unique flat partitions and aliases; never invent parent membership."""
    partitions, aliases, signatures = {}, {}, {}
    scales = {"default": {"communities": mapping["communities"]}}
    for value in mapping.get("community_scales", []):
        scale = value["scale"]
        if scale == "default":
            if value["communities"] != mapping["communities"]:
                raise ValueError("Default community mirror differs from the saved partition")
        elif scale in scales:
            raise ValueError("Duplicate community scale")
        else:
            scales[scale] = value
    for scale, value in scales.items():
        groups = value["communities"]
        signature = _digest(sorted((row["community_id"], sorted(row["source_ids"])) for row in groups))
        if signature in signatures:
            aliases[scale] = signatures[signature]
        else:
            partitions[scale] = groups
            aliases[scale] = signatures[signature] = scale
    return partitions, aliases


def prepare_label_update(mapping, corpus):
    partitions, aliases = naming_partitions(mapping)
    return {"mapping_digest": _digest(mapping), "corpus_digest": _digest(corpus),
            "aliases": aliases, "partitions": {
                scale: propose_community_labels(groups, corpus) for scale, groups in partitions.items()}}


def project_label_update(mapping, corpus, prepared, *, reviewed=None):
    """Apply only presentation fields, optionally with evidence-bound reviewed names.

    Reviewed records must contain exactly the community's sorted source_ids and
    the preparation input_identity. A review is provenance, not model certainty.
    """
    if prepared["mapping_digest"] != _digest(mapping) or prepared["corpus_digest"] != _digest(corpus):
        raise ValueError("Stale naming preparation")
    if prepared != prepare_label_update(mapping, corpus):
        raise ValueError("Naming preparation does not match its evidence")
    reviewed = reviewed or {}
    if set(reviewed) - set(prepared["partitions"]):
        raise ValueError("Unrequested naming partition")
    selected = {}
    for scale, proposal in prepared["partitions"].items():
        overrides = reviewed.get(scale, {})
        if set(overrides) - set(proposal["labels"]):
            raise ValueError("Unrequested community label")
        selected[scale] = {}
        used = set()
        for cid, row in proposal["labels"].items():
            record = overrides.get(cid)
            label, kind, identity = row["display_label"], "contrastive_terms", row["naming_identity"]
            if record is not None:
                if (record.get("input_identity") != proposal["input_identity"]
                        or record.get("source_ids") != row["source_ids"]):
                    raise ValueError("Stale or mismatched reviewed label evidence")
                label = record.get("title")
                if (not isinstance(label, str) or not label.strip() or len(label) > 100
                        or any(ord(char) < 32 for char in label)):
                    raise ValueError("Invalid reviewed label")
                kind, identity = "reviewed_contrastive", _digest(record)
            key = " ".join(unicodedata.normalize("NFKC", label).casefold().split())
            if key in used:
                raise ValueError("Duplicate sibling label requires review")
            used.add(key)
            selected[scale][cid] = {"display_label": label, "display_label_kind": kind,
                                    "naming_identity": identity}
    result = deepcopy(mapping)

    def apply(groups, scale):
        for row in groups:
            row.update(selected[prepared["aliases"][scale]][row["community_id"]])

    apply(result["communities"], "default")
    for value in result.get("community_scales", []):
        apply(value["communities"], value["scale"])
    return result
