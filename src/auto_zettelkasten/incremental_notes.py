"""Coalesced local note discovery, separate from canonical mapping results.

Enqueue only reads Markdown. Explicit refresh encodes a bounded number of
changed notes, reuses checked vectors, and replaces affected suggestion rows.
It never calls a provider, edits a note, or recomputes saved communities.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading
import time

from .documents import DocumentIndexChanged
from .local_mapping import LocalMappingOptions, SCHEMA_VERSION, _exclusive_cache, _numpy
from .note_recall import NoteRecallService, _CacheReader, _digest, _json, _safe_path, _snapshot_identity


_LOCK = threading.RLock()
_SCHEMA = "incremental-note-discovery-v1"
_MAX_BYTES = 48 * 1024 * 1024
_MAX_EDGES = 100_000


def _memberships(values):
    if (not isinstance(values, (list, tuple)) or len(values) > 64
            or any(not isinstance(value, str) or not 0 < len(value) <= 256
                   or any(ord(char) < 32 for char in value) for value in values)):
        raise ValueError("Discovery collection identities must be bounded strings.")
    return sorted(set(values))


class IncrementalNoteDiscovery:
    def __init__(self, private_root=None, *, recall=None):
        self.root = Path(private_root or Path.home() / ".config/auto-zettelkasten/incremental-notes").expanduser()
        self.recall = recall or NoteRecallService()

    @contextmanager
    def _state(self, workspace):
        root = Path(workspace).resolve(strict=True)
        info = root.stat()
        identity = {"path": str(root), "device": info.st_dev, "inode": info.st_ino}
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir():
            raise ValueError("Incremental state requires a private directory.")
        directory = _safe_path(self.root.resolve(), _digest(identity))
        directory.mkdir(mode=0o700, exist_ok=True)
        with _LOCK, _exclusive_cache(directory):
            path = directory / "state.json"
            if path.exists():
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > _MAX_BYTES:
                    raise ValueError("Invalid incremental discovery state file.")
                value = json.loads(path.read_bytes())
                payload = {k: v for k, v in value.items() if k != "sha256"}
                if (value.get("sha256") != _digest(payload) or value.get("schema") != _SCHEMA
                        or value.get("workspace") != identity or not isinstance(value.get("indexed"), dict)
                        or not isinstance(value.get("neighbors"), list)):
                    raise ValueError("Incremental discovery state failed validation.")
            else:
                value = {"schema": _SCHEMA, "workspace": identity, "indexed": {}, "pending": {},
                         "neighbors": [], "community_refresh_pending": False}
            yield root, path, value

    @staticmethod
    def _write(path, value):
        payload = {k: v for k, v in value.items() if k != "sha256"}
        encoded = (_json({**payload, "sha256": _digest(payload)}) + "\n").encode()
        if len(encoded) > _MAX_BYTES or len(value["neighbors"]) > _MAX_EDGES:
            raise ValueError("Incremental discovery storage bound reached; prior suggestions preserved.")
        descriptor, name = tempfile.mkstemp(prefix=".state-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)

    def _inventory(self, root, documents, workspace_id):
        if Path(documents.workspace_path(workspace_id)).resolve() != root:
            raise ValueError("Discovery workspace does not match its document service.")
        snapshot = documents.recall_corpus(workspace_id)
        if snapshot.get("truncated"):
            raise ValueError("Incomplete note inventory cannot remove or refresh discovery links.")
        corpus, raw, excluded = self.recall._corpus_for(root, snapshot)
        metadata = {row["path"]: row for row in snapshot["documents"]}
        inventory = {}
        for row in corpus:
            sid = row["source_id"]
            fields = metadata[row["note_path"]]["frontmatter"]
            inventory[sid] = {"path": row["note_path"], "semantic_sha256": raw[sid]["semantic_sha256"],
                              "collections": _memberships(fields.get("collection_keys", []))}
        return snapshot, corpus, raw, inventory, excluded

    @staticmethod
    def _queue(value, inventory):
        value["pending"] = {sid: row for sid, row in inventory.items() if value["indexed"].get(sid) != row}
        for edge in value["neighbors"]:
            source, target = edge["source_id"], edge["target_source_id"]
            if source in inventory and target in inventory and (
                    edge["source_hash"] != inventory[source]["semantic_sha256"]
                    or edge["target_hash"] != inventory[target]["semantic_sha256"]):
                value["pending"][source] = inventory[source]
        removed = set(value["indexed"]) - set(inventory)
        if removed:
            value["neighbors"] = [edge for edge in value["neighbors"]
                                  if edge["source_id"] not in removed and edge["target_source_id"] not in removed]
            for sid in removed:
                value["indexed"].pop(sid, None)
            value["community_refresh_pending"] = True
        if value["pending"]:
            value["community_refresh_pending"] = True

    def enqueue(self, workspace, *, documents, workspace_id):
        """Coalesce saves/external changes; does not open an encoder."""
        with self._state(workspace) as (root, path, value):
            _, _, _, inventory, _ = self._inventory(root, documents, workspace_id)
            self._queue(value, inventory)
            self._write(path, value)
            return {"pending_note_count": len(value["pending"]),
                    "community_refresh_pending": value["community_refresh_pending"], "provider_calls": 0}

    @staticmethod
    def _policy(root):
        from .local_tools import _ReadBudget
        budget = _ReadBudget(root, max_files=1, max_file_bytes=1024 * 1024, max_total_bytes=1024 * 1024)
        config = root / "auto-zettelkasten.yml"
        budget.reserve([config])
        options = LocalMappingOptions.from_mapping(budget.read(config).get("local_mapping", {}))
        source_memberships = {}
        mapping_path = _safe_path(root, ".auto-zettelkasten/mapping/result.json")
        if mapping_path.exists():
            reader = _CacheReader(root)
            saved = reader.object(mapping_path.relative_to(root), 64 * 1024 * 1024)
            identity = saved.get("identity", {})
            if (saved.get("schema_version") != SCHEMA_VERSION or not isinstance(identity, dict)
                    or identity.get("digest") != _digest({k: v for k, v in identity.items() if k != "digest"})):
                raise ValueError("Saved calibrated mapping policy failed identity validation.")
            tuning = identity.get("clustering", {})
            keys = ("neighbors", "min_similarity", "seed", "resolution", "min_cluster_size", "clustering")
            options = replace(options, **{key: tuning[key] for key in keys if key in tuning})
            options.validate()
            for source in saved.get("sources", []):
                source_memberships[source["source_id"]] = _memberships(source.get("collection_keys", []))
            if not reader.verify():
                raise DocumentIndexChanged()
        # A personal note can bridge collections regardless of its folder.
        return replace(options, discovery_scope="collection_and_library"), source_memberships

    def refresh(self, workspace, *, documents, workspace_id, model_path="", max_changed_notes=32,
                options=None, progress=None):
        if type(max_changed_notes) is not int or not 1 <= max_changed_notes <= 64:
            raise ValueError("A refresh admits 1–64 changed notes.")
        started = time.monotonic()
        with self._state(workspace) as (root, path, value):
            snapshot, corpus, raw, inventory, excluded = self._inventory(root, documents, workspace_id)
            self._queue(value, inventory)
            selected = sorted(value["pending"])[:max_changed_notes]
            if not selected:
                self._write(path, value)
                return {"status": "current", "updated_note_count": 0, "pending_note_count": 0,
                        "provider_calls": 0, "jev": {"enabled": False}, "seconds": time.monotonic() - started}
            policy, memberships = self._policy(root) if options is None else (LocalMappingOptions.from_mapping(options), {})
            policy = replace(policy, discovery_scope="collection_and_library")
            if len(inventory) > min(20_000, policy.max_sources):
                raise ValueError("Discovery corpus exceeds the existing local source bound.")
            self._write(path, value)  # Durable queue precedes potentially interrupted encoding.
            prepared = self.recall.prepare(root, documents=documents, workspace_id=workspace_id,
                model_path=model_path or policy.model_path or "", max_changed_notes=max_changed_notes,
                source_ids=selected, max_wall_seconds=policy.max_wall_seconds,
                min_free_disk_bytes=policy.min_free_disk_bytes, max_cache_bytes=policy.max_cache_bytes,
                progress=progress)
            vectors, _, coverage = self.recall._semantic(root, corpus, raw,
                self.recall._configured_model(root, model_path or policy.model_path or ""))
            ids = sorted(vectors)
            completed = [sid for sid in selected if sid in vectors]
            np = _numpy()
            matrix = np.asarray([vectors[sid] for sid in ids], dtype=np.float32)
            positions = {sid: index for index, sid in enumerate(ids)}
            memberships = {sid: set(inventory[sid]["collections"] or memberships.get(sid, [])) for sid in ids}
            refreshed = []
            for sid in completed:
                if time.monotonic() - started > policy.max_wall_seconds:
                    raise RuntimeError("Incremental discovery reached the local wall limit; prior links preserved.")
                if shutil.disk_usage(path.parent).free < policy.min_free_disk_bytes:
                    raise RuntimeError("Incremental discovery reached the free disk limit; prior links preserved.")
                scores = matrix @ matrix[positions[sid]]
                ranked = sorted((other for other in ids if other != sid and
                    float(scores[positions[other]]) > max(0., policy.min_similarity)),
                    key=lambda other: (-float(scores[positions[other]]), other))
                within = [other for other in ranked if memberships[sid] & memberships[other]]
                outside = [other for other in ranked if not memberships[sid] & memberships[other]]
                chosen = {}
                lanes = ((within[:(policy.neighbors + 1) // 2], "within_collection"),
                         ((outside + within)[:policy.neighbors // 2], "whole_library"))
                for index in range(policy.neighbors):
                    for lane, scope in lanes:
                        if index < len(lane):
                            chosen.setdefault(lane[index], set()).add(scope)
                for other in ranked:
                    if len(chosen) >= policy.neighbors:
                        break
                    chosen.setdefault(other, set()).add("whole_library")
                for other, scopes in chosen.items():
                    refreshed.append({"source_id": sid, "target_source_id": other,
                        "score": min(1., float(scores[positions[other]])),
                        "source_hash": inventory[sid]["semantic_sha256"],
                        "target_hash": inventory[other]["semantic_sha256"],
                        "discovery_scopes": sorted(scopes)})
            if _snapshot_identity(documents.recall_corpus(workspace_id)) != _snapshot_identity(snapshot):
                raise DocumentIndexChanged()
            # Reconsider incoming edges on the next explicit pass too. They
            # remain suppressed while their recorded endpoint hash is stale.
            value["neighbors"] = [edge for edge in value["neighbors"] if edge["source_id"] not in completed] + refreshed
            value["indexed"].update({sid: inventory[sid] for sid in completed})
            value["embedding_identity"] = coverage.get("embedding_identity")
            value["policy"] = {key: getattr(policy, key) for key in ("neighbors", "min_similarity", "resolution", "seed")}
            value["community_refresh_pending"] = True
            self._queue(value, inventory)
            self._write(path, value)
            return {"status": "partial" if value["pending"] else "refreshed", "updated_note_count": len(completed),
                    "pending_note_count": len(value["pending"]), "suggestion_count": len(refreshed),
                    "community_refresh_pending": True, "policy": value["policy"], "indexing": prepared,
                    "semantic_coverage": coverage, "excluded_notes": excluded, "seconds": time.monotonic() - started,
                    "provider_calls": 0, "jev": {"enabled": False, "provider_calls": 0}}

    def projection(self, workspace, *, documents, workspace_id):
        """Current path-bound suggestions for the existing graph, never citations."""
        with self._state(workspace) as (root, _path, value):
            snapshot, _, _, inventory, _ = self._inventory(root, documents, workspace_id)
            self._queue(value, inventory)
            edges = {}
            for row in value["neighbors"]:
                first, second = row["source_id"], row["target_source_id"]
                if (first not in inventory or second not in inventory
                        or row["source_hash"] != inventory[first]["semantic_sha256"]
                        or row["target_hash"] != inventory[second]["semantic_sha256"]):
                    continue
                pair = tuple(sorted((first, second)))
                identity = "semantic:incremental:" + _digest(pair)[:24]
                previous = edges.get(pair)
                if previous:
                    previous["score"] = max(previous["score"], row["score"])
                    previous["provenance"]["discovery_scopes"] = sorted(set(previous["provenance"]["discovery_scopes"]) | set(row["discovery_scopes"]))
                    continue
                edges[pair] = {"id": identity, "source": inventory[first]["path"], "target": inventory[second]["path"],
                    "type": "semantic_similarity", "derived": True, "decision_status": "suggested", "accepted": False,
                    "evidence_backed": False, "score": row["score"], "provenance": {
                        "kind": "local_embedding_similarity", "embedding_identity": value.get("embedding_identity"),
                        "discovery_scopes": row["discovery_scopes"], "accepted": False, "evidence_backed": False}}
            community = value.get("community_overlay")
            if community and (value["pending"] or community["source_hashes"] !=
                              {sid: row["semantic_sha256"] for sid, row in inventory.items()}):
                community = None
            if community:
                # Presentation edits may move offsets while leaving the
                # verified semantic inventory identical. Project the actual
                # current document revisions; never alter historical receipts.
                paths = {row["path"] for row in inventory.values()}
                community = {**community, "note_revisions": {row["path"]: row["revision"]
                             for row in snapshot["documents"] if row["path"] in paths}}
            return {"relationships": list(edges.values()), "pending_note_count": len(value["pending"]),
                    "community_refresh_pending": value["community_refresh_pending"], "provider_calls": 0,
                    "community_overlay": community}

    def refresh_communities(self, workspace, *, documents, workspace_id, model_path="", options=None):
        """Explicit coalesced local clustering; never encode missing sources."""
        from .community_naming import deterministic_community_label
        from .local_mapping import cluster_source_vectors
        started = time.monotonic()
        with self._state(workspace) as (root, path, value):
            snapshot, corpus, raw, inventory, _ = self._inventory(root, documents, workspace_id)
            self._queue(value, inventory)
            if value["pending"]:
                raise ValueError("Refresh pending note discovery before recomputing communities.")
            policy, memberships = self._policy(root) if options is None else (LocalMappingOptions.from_mapping(options), {})
            policy = replace(policy, discovery_scope="collection_and_library")
            vectors, _, coverage = self.recall._semantic(root, corpus, raw,
                self.recall._configured_model(root, model_path or policy.model_path or ""))
            if set(vectors) != set(inventory):
                raise ValueError("Community refresh needs current compatible vectors for every eligible note.")
            if shutil.disk_usage(path.parent).free < policy.min_free_disk_bytes:
                raise RuntimeError("Community refresh reached the free disk limit.")
            ids = sorted(vectors)
            memberships = {sid: inventory[sid]["collections"] or memberships.get(sid, []) for sid in ids}
            communities, _ = cluster_source_vectors(ids, [vectors[sid] for sid in ids], options=policy,
                embedding_identity=coverage.get("embedding_identity", ""), collection_memberships=memberships)
            if time.monotonic() - started > policy.max_wall_seconds:
                raise RuntimeError("Community refresh reached the local wall limit; prior communities preserved.")
            titles = {row["source_id"]: row["title"] for row in corpus}
            projected = []
            for group in communities:
                members = group["source_ids"]
                projected.append({"community_id": group["community_id"], "algorithm": group["algorithm"],
                    "paths": sorted(inventory[sid]["path"] for sid in members),
                    "display_label": deterministic_community_label([{"source_id": sid, "title": titles[sid]} for sid in members]),
                    "scale": "default", **({"noise": group["noise"]} if "noise" in group else {})})
            if _snapshot_identity(documents.recall_corpus(workspace_id)) != _snapshot_identity(snapshot):
                raise DocumentIndexChanged()
            value["community_overlay"] = {"status": "current", "identity_digest": _digest({
                "embedding_identity": coverage.get("embedding_identity"), "inventory": inventory,
                "neighbors": policy.neighbors, "threshold": policy.min_similarity, "resolution": policy.resolution}),
                "communities": projected, "community_scales": [],
                "source_hashes": {sid: row["semantic_sha256"] for sid, row in inventory.items()},
                "note_revisions": {row["path"]: row["revision"] for row in snapshot["documents"]
                                   if row["path"] in {note["path"] for note in inventory.values()}},
                "coverage": {"included": len(inventory), "unavailable": 0, "stale": 0, "unresolved": 0}}
            value["community_refresh_pending"] = False
            self._write(path, value)
            return {"status": "refreshed", "community_count": len(projected), "provider_calls": 0,
                    "seconds": time.monotonic() - started, "jev": {"enabled": False}}
