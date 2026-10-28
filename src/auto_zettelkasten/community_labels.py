"""Local, contrastive labels for an existing community partition.

Labels are presentation proposals, never changes to membership or graph edges.
Scoring compares phrase support across sibling communities. It cannot certify
that scholarly themes are mutually exclusive, or recognize every synonym.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
import hashlib
import json
import math
import re
from typing import Any


LABEL_VERSION = "contrastive-community-labels-v1"
MAX_SOURCES = 20_000
MAX_COMMUNITIES = 2048
MAX_TEXT_BYTES = 128 * 1024 * 1024
MAX_NOTE_BYTES = 2 * 1024 * 1024
MAX_PHRASES_PER_NOTE = 512
MAX_CANDIDATE_VOCABULARY = 250_000
_ID = re.compile(r"[A-Za-z0-9._:-]{1,256}\Z")
_WORD = re.compile(r"\b[^\W\d_]+(?:['’][^\W\d_]+)*\b", re.UNICODE)
_GAP = re.compile(r"[ \t\-–—’']*\Z")
_STOP = frozenset((
    "a an and are as at be been being but by can could did do does for from had has have "
    "he her his how i if in into is it its may might more most must not of on or our "
    "out over she should so some than that the their them then there these they this "
    "those through to under up us was we were what when where which who will with "
    "would you your de du des la le les et der die das und el los las un une en del "
    "argue argues argued arguing show shows shown demonstrate demonstrates demonstrated "
    "suggest suggests suggested provide provides provided discuss discusses discussed "
    "describe describes described examine examines examined explore explores explored "
    "consider considers considered identify identifies identified explain explains explained "
    "include includes included focus focuses focused remain remains use uses used using "
    "source sources author authors article articles paper papers note notes summary summaries "
    "thesis chapter chapters book books volume volumes edition editions introduction conclusion "
    "conclusions finding findings argument arguments evidence literature research study studies "
    "analysis analyses agenda main key central important substantive conceptual empirical "
    "section sections page pages printed recovered scope limitations limit limits based "
    "also rather instead both across within between whereas therefore however because "
    "ignore previous instructions instruction system prompt assistant return json output "
    "consensus unanimous proves supports contradicts demonstrates"
).split())


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _string(value: Any, name: str, limit: int, *, empty: bool = False) -> str:
    if (not isinstance(value, str) or len(value) > limit
            or any(ord(char) < 32 and char not in "\n\r\t" for char in value)
            or any(0xD800 <= ord(char) <= 0xDFFF for char in value)
            or (not empty and not value.strip())):
        raise ValueError(f"invalid {name}")
    return value


def _stem(word: str) -> str:
    word = word.casefold().replace("’", "'")
    if len(word) > 5 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith("s") and not word.endswith(("ss", "ics", "us")):
        return word[:-1]
    return word


def _phrase_key(phrase: str) -> str:
    return " ".join(_stem(match.group()) for match in _WORD.finditer(phrase))


def _display(phrase: str) -> str:
    phrase = " ".join(phrase.split()).strip(" -*_`'’\"“”")
    if phrase.isupper():
        phrase = " ".join(word if len(word) <= 4 else word.lower() for word in phrase.split())
    return phrase[:1].upper() + phrase[1:]


def _extract(text: str, field: str) -> dict[str, dict[str, Any]]:
    tokens = list(_WORD.finditer(text))
    if len(tokens) > 200_000:
        raise ValueError("community label per-note token limit exceeded")
    phrases: dict[str, dict[str, Any]] = {}
    for at, first in enumerate(tokens):
        if first.group().casefold() in _STOP or len(first.group()) < 3:
            continue
        for length in (2, 3, 4):
            end = at + length
            if end > len(tokens):
                break
            window = tokens[at:end]
            if any(not _GAP.fullmatch(text[left.end():right.start()]) for left, right in zip(window, window[1:])):
                break
            words = [match.group().casefold() for match in window]
            if words[-1] in _STOP or len(words[-1]) < 3 or sum(word in _STOP for word in words) > length // 2:
                continue
            if any(word in {"ignore", "instructions", "prompt", "assistant", "json", "consensus", "unanimous"} for word in words):
                continue
            phrase = text[first.start():window[-1].end()]
            if len(phrase) > 100:
                continue
            key = _phrase_key(phrase)
            item = phrases.setdefault(key, {"count": 0, "phrase": phrase, "field": field,
                                            "start": first.start(), "end": window[-1].end()})
            item["count"] += 1
    return phrases


def _overlap(left: str, right: str) -> float:
    a, b = set(left.split()) - _STOP, set(right.split()) - _STOP
    if not a or not b:
        return 0.
    if a <= b or b <= a:
        return 1.
    return len(a & b) / len(a | b)


def propose_community_labels(
    communities: Sequence[Mapping[str, Any]], corpus: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Propose grounded labels jointly for one unchanged partition/scale.

    ``corpus`` binds exact cleaned note text with ``content_sha256``. Outsiders
    are allowed for a selected subset, but only supplied community members enter
    sibling statistics. Communities must have distinct IDs and disjoint, known,
    nonempty membership. Identical or indistinguishable topics remain unresolved
    instead of receiving manufactured distinctions or numbered suffixes.
    """
    if isinstance(communities, (str, bytes)) or not isinstance(communities, Sequence) or len(communities) > MAX_COMMUNITIES:
        raise ValueError("community label community limit exceeded")
    if isinstance(corpus, (str, bytes)) or not isinstance(corpus, Sequence) or len(corpus) > MAX_SOURCES:
        raise ValueError("community label corpus limit exceeded")
    rows, total_bytes = {}, 0
    for raw in corpus:
        if not isinstance(raw, Mapping):
            raise ValueError("community label corpus rows must be objects")
        sid = _string(raw.get("source_id"), "source_id", 256)
        if not _ID.fullmatch(sid) or sid in rows:
            raise ValueError("duplicate or invalid community label source identity")
        text = _string(raw.get("text"), "source text", MAX_NOTE_BYTES, empty=True)
        title = _string(raw.get("title", sid), "source title", 2000)
        size = len(text.encode("utf-8"))
        total_bytes += size
        if size > MAX_NOTE_BYTES or total_bytes > MAX_TEXT_BYTES:
            raise ValueError("community label text byte limit exceeded")
        if raw.get("content_sha256") != hashlib.sha256(text.encode("utf-8")).hexdigest():
            raise ValueError("community label source text hash mismatch")
        rows[sid] = {"source_id": sid, "text": text, "title": title, "content_sha256": raw["content_sha256"]}
    groups, owner = {}, {}
    for raw in communities:
        if not isinstance(raw, Mapping):
            raise ValueError("communities must be objects")
        cid = _string(raw.get("community_id"), "community_id", 256)
        ids = raw.get("source_ids")
        if not _ID.fullmatch(cid) or cid in groups:
            raise ValueError("duplicate or invalid community identity")
        if (not isinstance(ids, (list, tuple)) or not ids or len(ids) > MAX_SOURCES
                or any(not isinstance(sid, str) or sid not in rows for sid in ids) or len(set(ids)) != len(ids)):
            raise ValueError("community membership must contain distinct known source identities")
        if any(sid in owner for sid in ids):
            raise ValueError("a source belongs to multiple sibling communities")
        fallback = raw.get("display_label", "Community topics")
        _string(fallback, "fallback display label", 160)
        groups[cid] = {"source_ids": sorted(ids), "fallback": fallback}
        owner.update((sid, cid) for sid in ids)
    input_identity = _digest({"version": LABEL_VERSION, "communities": {cid: {"source_ids": row["source_ids"], "fallback": row["fallback"]} for cid, row in sorted(groups.items())},
                              "sources": {sid: {"title": rows[sid]["title"], "content_sha256": rows[sid]["content_sha256"]} for sid in sorted(owner)}})
    per_note, examples, omitted = {}, {}, 0
    for sid in sorted(owner):
        title = _extract(rows[sid]["title"], "title")
        body = _extract(rows[sid]["text"], "text")
        values = {}
        for key in title.keys() | body.keys():
            entry = title.get(key) or body[key]
            weight = 4 * int(key in title) + math.log1p(body.get(key, {}).get("count", 0))
            values[key] = {**entry, "weight": weight, "title_support": key in title}
        retained = sorted(values, key=lambda key: (-values[key]["weight"], key))[:MAX_PHRASES_PER_NOTE]
        omitted += len(values) - len(retained)
        per_note[sid] = {key: values[key] for key in retained}
        for key in retained:
            examples.setdefault(key, []).append((not values[key]["title_support"], sid, values[key]))
        if len(examples) > MAX_CANDIDATE_VOCABULARY:
            raise ValueError("community label candidate vocabulary limit exceeded")
    weights, supports, title_support = {}, {}, {}
    for cid, group in sorted(groups.items()):
        weighted, support, titled = Counter(), defaultdict(set), defaultdict(set)
        for sid in group["source_ids"]:
            for key, entry in per_note[sid].items():
                weighted[key] += entry["weight"]
                support[key].add(sid)
                if entry["title_support"]:
                    titled[key].add(sid)
        weights[cid], supports[cid], title_support[cid] = weighted, support, titled
    total_weights = Counter()
    for values in weights.values():
        total_weights.update(values)
    average_words = sum(sum(values.values()) for values in weights.values()) / max(1, len(groups))
    # Only the strongest two group supports are needed to find the strongest
    # competitor. Avoid scanning every community for every phrase candidate.
    competition = defaultdict(list)
    for cid, support in supports.items():
        for key, ids in support.items():
            competition[key].append((len(ids) / len(groups[cid]["source_ids"]), cid))
    competition = {key: sorted(values, reverse=True)[:2] for key, values in competition.items()}
    candidates, reasons = {}, {}
    for cid, group in sorted(groups.items()):
        candidate_rows = []
        denominator = sum(weights[cid].values()) or 1.
        for key, support in supports[cid].items():
            coverage = len(support) / len(group["source_ids"])
            outside, neighbor = next((value for value in competition[key] if value[1] != cid), (0., None))
            distinct = max(0., (coverage - outside) / max(coverage, 1e-12))
            # Do not turn the same shared subject into differently worded,
            # purportedly exclusive labels. Keep ambiguity explicit.
            if distinct <= .05:
                continue
            ctfidf = weights[cid][key] / denominator * math.log1p(average_words / max(total_weights[key], 1e-12))
            title_coverage = len(title_support[cid].get(key, ())) / len(group["source_ids"])
            phrase_words = len(key.split())
            score = ctfidf * coverage ** 1.5 * (1 + title_coverage) * (.1 + .9 * distinct) * (1 + .08 * (phrase_words - 2))
            example = sorted((item for item in examples[key] if item[1] in support), key=lambda item: (item[0], item[1]))[0]
            original = example[2]
            candidate_rows.append({"phrase": original["phrase"], "label": _display(original["phrase"]), "key": key,
                "score": round(score, 12), "ctfidf_score": round(ctfidf, 12),
                "support_source_ids": sorted(support), "support_count": len(support), "coverage": round(coverage, 6),
                "title_support_count": len(title_support[cid].get(key, ())), "distinctiveness": round(distinct, 6),
                "neighbor_community_id": neighbor, "neighbor_support": round(outside, 6),
                "evidence": [{"source_id": example[1], "field": original["field"], "start": original["start"],
                              "end": original["end"], "text": original["phrase"],
                              "content_sha256": rows[example[1]]["content_sha256"]}]})
        candidates[cid] = sorted(candidate_rows, key=lambda row: (-row["score"], -row["coverage"], row["key"]))[:24]
        reasons[cid] = [] if candidates[cid] else ["no_distinguishing_supported_phrase"]
    # Assign sibling labels jointly, with deterministic ordering and a penalty
    # for redundant wording. This is lexical distinction, not a semantic proof.
    chosen, assigned = {}, []
    order = sorted(groups, key=lambda cid: (-candidates[cid][0]["score"] if candidates[cid] else 0., cid))
    for cid in order:
        ranked = []
        for candidate in candidates[cid]:
            similarity = max((_overlap(candidate["key"], prior) for prior in assigned), default=0.)
            if similarity >= .8:
                continue
            ranked.append((candidate["score"] * (1 - .65 * similarity), candidate))
        if not ranked:
            chosen[cid] = None
            if candidates[cid]:
                reasons[cid].append("sibling_label_collision")
        else:
            selected = sorted(ranked, key=lambda pair: (-pair[0], pair[1]["key"]))[0][1]
            chosen[cid] = selected
            assigned.append(selected["key"])
            if selected["coverage"] < .5:
                reasons[cid].append("limited_member_coverage")
    labels = {}
    for cid, group in sorted(groups.items()):
        selected = chosen[cid]
        mixed = bool(selected and len(group["source_ids"]) >= 3 and selected["coverage"] < .5)
        if mixed:
            reasons[cid].append("weak_shared_phrase_coverage")
        phrase_scores = defaultdict(float)
        for candidate in candidates[cid][:8]:
            for sid in candidate["support_source_ids"]:
                phrase_scores[sid] += candidate["score"]
        representatives = sorted(group["source_ids"], key=lambda sid: (-phrase_scores[sid], sid))[:4]
        label = selected["label"] if selected and not mixed else group["fallback"]
        record = {"community_id": cid, "source_ids": group["source_ids"], "display_label": label,
                  "status": "mixed" if mixed else "proposed" if selected else "unresolved", "method": LABEL_VERSION,
                  "candidates": candidates[cid][:8], "selected_candidate": selected,
                  "representative_source_ids": representatives,
                  "diagnostics": {"reasons": reasons[cid], "semantic_exclusivity_certified": False,
                                  "member_count": len(group["source_ids"]), "fallback_retained": selected is None or mixed,
                                  "selected_phrase_member_scores": {sid: int(selected is not None and sid in selected["support_source_ids"]) for sid in group["source_ids"]},
                                  "uncovered_source_ids": [sid for sid in group["source_ids"] if selected is None or sid not in selected["support_source_ids"]]}}
        record["naming_identity"] = _digest({"input_identity": input_identity, "community_id": cid, "display_label": label,
                                            "selected_phrase_key": selected["key"] if selected else None})
        labels[cid] = record
    duplicates = defaultdict(list)
    for cid, row in labels.items():
        duplicates[_phrase_key(row["display_label"])].append(cid)
    unresolved = [cid for cid, row in labels.items() if row["status"] != "proposed"]
    return {"version": LABEL_VERSION, "input_identity": input_identity, "labels": labels,
            "diagnostics": {"status": "empty" if not groups else "review_required" if unresolved else "proposed",
                "community_count": len(groups), "member_source_count": len(owner), "outside_corpus_source_count": len(rows) - len(owner),
                "unresolved_community_ids": unresolved, "duplicate_label_groups": [ids for _, ids in sorted(duplicates.items()) if len(ids) > 1],
                "phrase_vocabulary": len(examples), "omitted_per_note_candidates": omitted,
                "max_phrases_per_note": MAX_PHRASES_PER_NOTE, "model_calls": 0,
                "membership_changed": False, "links_changed": False,
                "limitations": ["Lexical sibling contrast, not proof of mutually exclusive themes.",
                                "Phrase extraction has no grammatical parser or semantic synonym verifier.",
                                "Low-coverage or unresolved labels need review; groups are never split or merged."]}}
