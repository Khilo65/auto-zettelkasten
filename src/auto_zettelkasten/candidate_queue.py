"""Local durable comparison progress, separate from canonical relationships.

Only caller-validated evidence identities and decisions enter this cache. Ranking
changes preserve decisions; changed evidence/decision contracts create fresh work.
No queue status is itself a claim that a substantive relationship exists.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import shutil
from typing import Any, Mapping, Sequence

from .workspace import resolve_workspace
from .retrieval import CandidateResourceError


QUEUE_SCHEMA = "candidate-continuation-v1"
_MAX_CACHE_BYTES = 1024 * 1024 * 1024
_MIN_FREE_BYTES = 1024 * 1024 * 1024
_TERMINAL = {"accepted", "negative", "deferred", "unknown_dispatch", "resolved"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def queue_scope(instance_id: str | None, library_id: str | None, source_ids: Sequence[str] | None) -> str:
    return hashlib.sha256(_json([instance_id, library_id, None if source_ids is None else sorted(set(source_ids))]).encode()).hexdigest()


class CandidateQueue:
    def __init__(self, workspace: Path | str, scope: str):
        self.root = resolve_workspace(workspace)
        directory = self.root / ".auto-zettelkasten"
        if directory.is_symlink():
            raise ValueError("candidate cache cannot be a symbolic link")
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "candidate-queue.sqlite3"
        if self.path.is_symlink() or not isinstance(scope, str) or len(scope) != 64:
            raise ValueError("candidate queue needs a confined path and scope identity")
        self.scope = scope
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS queue_progress(scope TEXT PRIMARY KEY, completed_passes INTEGER NOT NULL DEFAULT 0)")
            db.execute("INSERT OR IGNORE INTO queue_progress(scope) VALUES(?)", (self.scope,))
            db.execute("""CREATE TABLE IF NOT EXISTS candidates (
                scope TEXT NOT NULL, left_id TEXT NOT NULL, right_id TEXT NOT NULL,
                evidence_identity TEXT NOT NULL, payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'unexamined', outcome TEXT NOT NULL DEFAULT '{}',
                eligible INTEGER NOT NULL DEFAULT 1, rank INTEGER NOT NULL,
                selections INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(scope,left_id,right_id,evidence_identity))""")

    @contextmanager
    def _connect(self):
        if self.path.is_symlink():
            raise ValueError("candidate queue cannot be a symbolic link")
        db = sqlite3.connect(self.path, timeout=30)
        try:
            page_size = db.execute("PRAGMA page_size").fetchone()[0]
            db.execute(f"PRAGMA max_page_count={_MAX_CACHE_BYTES // page_size}")
            with db:
                yield db
        except sqlite3.OperationalError as exc:
            if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL:
                raise CandidateResourceError("candidate queue reached its disk/cache bound; saved progress preserved") from exc
            raise
        finally:
            db.close()

    def sync(self, candidates: Sequence[Mapping[str, Any]], *, bindings: Mapping[str, Any],
             contract: Mapping[str, Any], resolved_pairs: Sequence[tuple[str, str]] = ()) -> None:
        if len(candidates) > 100_000:
            raise CandidateResourceError("candidate queue exceeds the bounded 100000-pair pool")
        if shutil.disk_usage(self.root).free < _MIN_FREE_BYTES:
            raise CandidateResourceError("candidate queue stopped: less than 1 GiB of free disk space")
        if self.path.stat().st_size > _MAX_CACHE_BYTES:
            raise CandidateResourceError("candidate queue exceeds its 1 GiB cache bound")
        resolved = {tuple(sorted(pair)) for pair in resolved_pairs}
        prepared = []
        for rank, raw in enumerate(candidates, 1):
            left, right = raw["left_source_id"], raw["right_source_id"]
            if not isinstance(left, str) or not isinstance(right, str) or not left or left >= right or len(left) > 256 or len(right) > 256:
                raise ValueError("candidate queue requires canonical distinct endpoints")
            if left not in bindings or right not in bindings:
                raise ValueError("candidate queue requires current endpoint bindings")
            # Discovery scores/ranks/provenance never invalidate an unchanged
            # decision. Evidence and its interpretation contract do.
            identity = hashlib.sha256(_json({"endpoints": {left: bindings[left], right: bindings[right]}, "contract": contract}).encode()).hexdigest()
            payload = {**dict(raw), "queue_evidence_identity": identity}
            encoded = _json(payload)
            if len(encoded.encode()) > 32_768:
                raise ValueError("candidate queue payload exceeds its bound")
            prepared.append((self.scope, left, right, identity, encoded, rank))
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE candidates SET eligible=0 WHERE scope=?", (self.scope,))
            db.executemany("""INSERT INTO candidates(scope,left_id,right_id,evidence_identity,payload,rank)
                VALUES(?,?,?,?,?,?) ON CONFLICT(scope,left_id,right_id,evidence_identity)
                DO UPDATE SET eligible=1,payload=excluded.payload,rank=excluded.rank""", prepared)
            db.execute("UPDATE candidates SET status='unexamined' WHERE scope=? AND eligible=1 AND status='resolved'", (self.scope,))
            for left, right in resolved:
                db.execute("UPDATE candidates SET status='resolved' WHERE scope=? AND left_id=? AND right_id=? AND eligible=1 AND status='unexamined'", (self.scope, left, right))

    def select(self, *, per_source_cap: int, max_pairs: int,
               preferred_pairs: Sequence[tuple[str, str]] | None = None) -> list[dict[str, Any]]:
        if type(per_source_cap) is not int or not 1 <= per_source_cap <= 256 or type(max_pairs) is not int or not 1 <= max_pairs <= 100_000:
            raise ValueError("invalid candidate selection limits")
        counts: Counter[str] = Counter()
        selected = []
        with self._connect() as db:
            passes = db.execute("SELECT completed_passes FROM queue_progress WHERE scope=?", (self.scope,)).fetchone()[0]
            initial = None if preferred_pairs is None or passes else {tuple(sorted(pair)) for pair in preferred_pairs}
            rows = db.execute("SELECT left_id,right_id,payload FROM candidates WHERE scope=? AND eligible=1 AND status='unexamined' ORDER BY selections,rank,left_id,right_id", (self.scope,))
            for left, right, encoded in rows:
                if initial is not None and (left, right) not in initial:
                    continue
                if counts[left] >= per_source_cap or counts[right] >= per_source_cap:
                    continue
                selected.append({**json.loads(encoded), "rank": len(selected) + 1})
                counts.update((left, right))
                if len(selected) >= max_pairs:
                    break
        return selected

    def record(self, candidate: Mapping[str, Any], status: str, *, detail: Mapping[str, Any] | None = None) -> None:
        if status not in _TERMINAL | {"unexamined"}:
            raise ValueError("invalid candidate progress status")
        encoded = _json(detail or {})
        if len(encoded.encode()) > 8192:
            raise ValueError("candidate outcome summary exceeds its bound")
        with self._connect() as db:
            updated = db.execute("""UPDATE candidates SET status=?,outcome=?,selections=selections+1
                WHERE scope=? AND left_id=? AND right_id=? AND evidence_identity=? AND eligible=1""",
                (status, encoded, self.scope, candidate["left_source_id"], candidate["right_source_id"], candidate["queue_evidence_identity"]))
            if updated.rowcount != 1:
                raise ValueError("candidate outcome does not match current queued evidence")

    def finish_pass(self) -> None:
        """Advance admission after an executed pass, including an empty shortlist.

        Dry-run callers must never call this. No decision or relationship is
        inferred merely because a pass finished.
        """
        with self._connect() as db:
            db.execute("UPDATE queue_progress SET completed_passes=completed_passes+1 WHERE scope=?", (self.scope,))

    def summary(self) -> dict[str, Any]:
        with self._connect() as db:
            counts = dict(db.execute("SELECT status,COUNT(*) FROM candidates WHERE scope=? AND eligible=1 GROUP BY status", (self.scope,)))
            passes = db.execute("SELECT completed_passes FROM queue_progress WHERE scope=?", (self.scope,)).fetchone()[0]
            historical = db.execute("SELECT COUNT(*) FROM candidates WHERE scope=? AND eligible=0", (self.scope,)).fetchone()[0]
        return {"schema": QUEUE_SCHEMA, "counts": counts, "completed_passes": passes, "total": sum(counts.values()),
                "unexamined_count": counts.get("unexamined", 0),
                "unfinished": bool(counts.get("unexamined", 0) or counts.get("unknown_dispatch", 0)),
                "historical_count": historical, "all_pairs_examined": False}
