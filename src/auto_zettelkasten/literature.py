from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

from .files import atomic_write_text, now_iso, read_yaml, sha256_text, slugify, write_yaml


def build_literature_map(
    workspace: Path,
    *,
    source_set: Mapping[str, Any],
    notes: Sequence[Mapping[str, Any]],
    question: str | None,
    run_id: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[Path]]:
    clusters, rejected = _clusters(notes)
    cluster_paths = _write_clusters(workspace, clusters, rejected)
    gaps = _gaps(clusters, notes, question)
    gap_paths = _write_gaps(workspace, gaps)
    packet = _write_packet(workspace, source_set, clusters, gaps, run_id, question)
    cluster_map = {
        "status": "built" if clusters else "blocked_insufficient_source_memory",
        "clusters": clusters,
        "rejected_proposals": rejected,
        "minimum_analytical_notes": 2,
        "path": str(workspace / "03_literature_synthesis" / "clusters" / "clusters.yml"),
    }
    gap_map = {
        "status": "candidate_only" if gaps else "blocked_no_source_backed_clusters",
        "gap_candidates": gaps,
        "novelty_claimed": False,
        "path": str(workspace / "03_literature_synthesis" / "gaps" / "gaps.yml"),
    }
    return cluster_map, gap_map, packet, [*cluster_paths, *gap_paths, Path(packet["path"])]


def _clusters(notes: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_tag: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for note in notes:
        if note.get("note_status") not in {"analytical_atomic_note", "verified_atomic_note"}:
            continue
        for tag in note.get("normalized_tags", []) or []:
            by_tag[str(tag)].append(note)
    clusters: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen_memberships: set[tuple[str, ...]] = set()
    for tag, members in sorted(by_tag.items()):
        unique = {str(row["note_id"]): row for row in members}
        member_ids = tuple(sorted(unique))
        if len(member_ids) < 2:
            rejected.append({"label": tag, "note_ids": list(member_ids), "action": "reject", "reason": "singleton_cluster"})
            continue
        if member_ids in seen_memberships:
            continue
        seen_memberships.add(member_ids)
        sources = [unique[note_id] for note_id in member_ids]
        cluster_id = f"cluster-{slugify(tag)}-{sha256_text('|'.join(member_ids))[:8]}"
        clusters.append(
            {
                "cluster_id": cluster_id,
                "label": tag.replace("-", " ").title(),
                "shared_question": f"How does the literature address {tag.replace('-', ' ')}?",
                "shared_concepts": [tag],
                "shared_methods": sorted({str(row.get("method", "")) for row in sources if row.get("method")}),
                "note_ids": list(member_ids),
                "source_ids": [str(row["source_id"]) for row in sources],
                "representative_sources": [
                    {
                        "note_id": row["note_id"],
                        "source_id": row["source_id"],
                        "title": row.get("title", ""),
                        "note_path": row.get("note_path", ""),
                        "note_hash": row.get("note_hash", ""),
                    }
                    for row in sources
                ],
                "status": "canonical",
                "source_backed": True,
                "created_at": now_iso(),
            }
        )
    return clusters, rejected


def _gaps(
    clusters: Sequence[Mapping[str, Any]],
    notes: Sequence[Mapping[str, Any]],
    question: str | None,
) -> list[dict[str, Any]]:
    note_map = {str(row["note_id"]): row for row in notes}
    gaps: list[dict[str, Any]] = []
    for rank, cluster in enumerate(clusters, start=1):
        members = [note_map[note_id] for note_id in cluster.get("note_ids", []) if note_id in note_map]
        prior = sorted(members, key=_prior_sort_key, reverse=True)[:3]
        gap_id = f"gap-{slugify(str(cluster['cluster_id']).removeprefix('cluster-'))}"
        closest = [
            {
                "source_id": row.get("source_id", ""),
                "note_id": row.get("note_id", ""),
                "title": row.get("title", ""),
                "note_path": row.get("note_path", ""),
                "note_hash": row.get("note_hash", ""),
                "selection_basis": "recent source-backed member of the supporting cluster",
            }
            for row in prior
        ]
        gaps.append(
            {
                "gap_id": gap_id,
                "rank": rank,
                "gap_type": "candidate_literature_gap",
                "gap_text": f"Candidate gap requiring falsification: unresolved tensions or omissions within {cluster['label']}.",
                "related_clusters": [cluster["cluster_id"]],
                "supporting_source_ids": list(cluster.get("source_ids", [])),
                "closest_prior_work": closest,
                "closest_prior_review": {
                    "status": "candidate_requires_review",
                    "decision": "unreviewed",
                    "provenance_count": len(closest),
                    "question_lens": question or "",
                    "why_not_already_answered": "Not established; this candidate must be checked against the cited closest-prior sources.",
                },
                "status": "candidate",
                "novelty_claimed": False,
                "qualification_gate": "source-backed cluster plus completed closest-prior review",
                "created_at": now_iso(),
            }
        )
    return gaps


def _write_clusters(workspace: Path, clusters: Sequence[Mapping[str, Any]], rejected: Sequence[Mapping[str, Any]]) -> list[Path]:
    root = workspace / "03_literature_synthesis" / "clusters"
    root.mkdir(parents=True, exist_ok=True)
    for stale in root.glob("cluster-*.md"):
        stale.unlink()
    registry = root / "clusters.yml"
    updates = root / "cluster_updates.yml"
    write_yaml(registry, {"updated_at": now_iso(), "minimum_analytical_notes": 2, "clusters": list(clusters)})
    write_yaml(updates, {"updated_at": now_iso(), "updates": list(rejected)})
    paths = [registry, updates]
    index_lines = ["# Cluster Index", ""]
    for cluster in clusters:
        path = root / f"{cluster['cluster_id']}.md"
        sources = "\n".join(
            f"- [[{Path(str(row.get('note_path'))).stem}]] — {row.get('title', '')}"
            for row in cluster.get("representative_sources", [])
        )
        text = (
            f"---\ncluster_id: {cluster['cluster_id']}\nlabel: {cluster['label']}\nstatus: canonical\n---\n\n"
            f"# Cluster Map: {cluster['label']}\n\n## Scope\n\n{cluster['shared_question']}\n\n"
            f"## Canonical and Representative Sources\n\n{sources}\n\n"
            "## Main Positions\n\nSee the linked source-faithful atomic notes. This map does not promote claims.\n\n"
            "## Mixed or Conditional Findings\n\nRequires comparative review.\n\n"
            "## Conceptual and Methodological Differences\n\nRequires comparative review.\n\n"
            "## Closest Prior Work\n\nSee candidate-gap provenance records.\n\n"
            "## Gaps and Under-Specified Issues\n\nOnly candidate gaps are generated automatically.\n\n"
            "## Coverage Status and Remaining Risks\n\nCanonical cluster membership requires at least two validated analytical notes.\n"
        )
        atomic_write_text(path, text)
        index_lines.append(f"- [[{cluster['cluster_id']}|{cluster['label']}]] ({len(cluster['note_ids'])} notes)")
        paths.append(path)
    index_path = root / "INDEX.md"
    atomic_write_text(index_path, "\n".join(index_lines) + "\n")
    paths.append(index_path)
    render_cluster_expansion_navigation(workspace)
    return paths


def render_cluster_expansion_navigation(workspace: Path) -> list[Path]:
    """Project scoped suggestions onto generated cluster pages as non-members."""

    payload = read_yaml(
        workspace / "03_literature_synthesis" / "expansion" / "candidates.yml",
        {},
    ) or {}
    candidates = payload.get("candidates", []) if isinstance(payload, Mapping) else []
    rows = [row for row in candidates if isinstance(row, Mapping)] if isinstance(candidates, list) else []
    cluster_root = workspace / "03_literature_synthesis" / "clusters"
    paths: list[Path] = []
    for path in sorted(cluster_root.glob("cluster-*.md")):
        cluster_id = path.stem
        related = [
            row
            for row in rows
            if cluster_id
            in (
                [str(value) for value in row.get("related_cluster_ids", [])]
                if isinstance(row.get("related_cluster_ids"), list)
                else []
            )
        ]
        lines = ["## Expansion Suggestions (Non-Members)", ""]
        if related:
            for row in sorted(
                related,
                key=lambda value: (-_float(value.get("score")), str(value.get("suggestion_id", ""))),
            ):
                suggestion_id = str(row.get("suggestion_id", ""))
                alias = _safe_expansion_title(str(row.get("title") or row.get("work_id") or suggestion_id))
                relation = str(row.get("primary_relation", ""))
                state = str(row.get("state", "proposed"))
                lines.append(
                    f"- [[{suggestion_id}|{alias}]] — discovery `{relation}`; "
                    f"decision `{state}`; non-member"
                )
        else:
            lines.append("No graph-expansion suggestions are associated with this cluster.")
        current = path.read_text(encoding="utf-8")
        base = re.sub(
            r"\n*## Expansion Suggestions \(Non-Members\)\s*\n.*\Z",
            "",
            current,
            flags=re.DOTALL,
        ).rstrip()
        atomic_write_text(path, base + "\n\n" + "\n".join(lines) + "\n")
        paths.append(path)
    return paths


def _safe_expansion_title(value: str) -> str:
    text = re.sub(r"\s+", " ", value).strip()
    return text.replace("[[", "[").replace("]]", "]").replace("|", "-")[:500] or "Untitled suggestion"


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _write_gaps(workspace: Path, gaps: Sequence[Mapping[str, Any]]) -> list[Path]:
    root = workspace / "03_literature_synthesis" / "gaps"
    candidates = root / "candidates"
    prior_root = workspace / "03_literature_synthesis" / "closest_prior_work"
    candidates.mkdir(parents=True, exist_ok=True)
    prior_root.mkdir(parents=True, exist_ok=True)
    for stale in candidates.glob("gap-*.md"):
        stale.unlink()
    for stale in prior_root.glob("gap-*.md"):
        stale.unlink()
    registry = root / "gaps.yml"
    compatibility_index = workspace / "02_source_memory" / "indexes" / "gap_candidates.yml"
    payload = {
        "updated_at": now_iso(),
        "status": "candidate_only" if gaps else "blocked_no_source_backed_clusters",
        "novelty_claimed": False,
        "gap_candidates": list(gaps),
    }
    write_yaml(registry, payload)
    write_yaml(compatibility_index, payload)
    paths = [registry, compatibility_index]
    index_lines = ["# Gap Candidate Index", "", "Candidates are not verified novelty claims.", ""]
    for gap in gaps:
        candidate_path = candidates / f"{gap['gap_id']}.md"
        prior_links = "\n".join(
            f"- [[{Path(str(row.get('note_path'))).stem}]] — {row.get('selection_basis', '')}"
            for row in gap.get("closest_prior_work", [])
        )
        text = (
            f"---\ngap_id: {gap['gap_id']}\nstatus: candidate\nnovelty_claimed: false\nrank: {gap['rank']}\n---\n\n"
            f"# Candidate Gap: {gap['gap_text']}\n\n## Supporting Clusters\n\n"
            + "\n".join(f"- [[{cluster_id}]]" for cluster_id in gap.get("related_clusters", []))
            + f"\n\n## Closest Prior Work and Provenance\n\n{prior_links}\n\n"
            "## Falsification Status\n\nUnreviewed. Do not describe this candidate as a verified gap or novelty claim.\n"
        )
        atomic_write_text(candidate_path, text)
        prior_path = prior_root / f"{gap['gap_id']}.md"
        atomic_write_text(
            prior_path,
            f"# Closest Prior Review: {gap['gap_id']}\n\nStatus: candidate requires review.\n\n{prior_links}\n",
        )
        index_lines.append(f"- [[{gap['gap_id']}]] — rank {gap['rank']}, candidate")
        paths.extend([candidate_path, prior_path])
    index_path = root / "INDEX.md"
    atomic_write_text(index_path, "\n".join(index_lines) + "\n")
    paths.append(index_path)
    return paths


def _write_packet(
    workspace: Path,
    source_set: Mapping[str, Any],
    clusters: Sequence[Mapping[str, Any]],
    gaps: Sequence[Mapping[str, Any]],
    run_id: str,
    question: str | None,
) -> dict[str, Any]:
    packet_id = f"literature-packet-{slugify(run_id)}"
    payload = {
        "packet_id": packet_id,
        "status": "ready" if clusters else "limited",
        "packet_kind": "literature_map",
        "source_set_id": source_set.get("source_set_id", ""),
        "cluster_ids": [row["cluster_id"] for row in clusters],
        "gap_ids": [row["gap_id"] for row in gaps],
        "question": question or "",
        "not_method_ready_bundle": True,
        "not_manuscript_text": True,
        "dependency_hash": sha256_text(str(source_set.get("dependency_hash", "")) + repr(clusters) + repr(gaps)),
        "created_at": now_iso(),
    }
    path = workspace / "03_literature_synthesis" / "packets" / f"{packet_id}.yml"
    write_yaml(path, payload)
    return {**payload, "path": str(path)}


def _prior_sort_key(note: Mapping[str, Any]) -> tuple[int, str]:
    match = re.search(r"(?:19|20)\d{2}", str(note.get("date", "")))
    return (int(match.group(0)) if match else 0, str(note.get("title", "")))
