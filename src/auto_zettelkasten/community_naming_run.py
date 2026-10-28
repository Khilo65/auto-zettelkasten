"""Opt-in two-stage naming execution with a separate durable spending ceiling.

Cache reuse and previews never construct a provider or read credentials. A
caller injects a matching naming reader only after explicit cloud permission.
All failed or unknown dispatched outcomes retain their full reservation.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from decimal import Decimal
import os
from pathlib import Path
import sqlite3
import stat

from .community_naming import _digest, _json, _money
from .community_naming_pipeline import (
    VERSION, MAX_INPUT_TOKENS, LOCAL_OUTPUT_TOKENS, RECONCILIATION_OUTPUT_TOKENS,
    _verify_preview, finish_reconciliation, validate_local_result, validate_final_result,
)


RUN_VERSION = "two-stage-community-naming-run-v1"


def _job_bounds(job):
    payload = job["request_payload"]
    if job.get("stage") not in {"local", "reconciliation"} or job["identity"].get("stage") != job["stage"]:
        raise ValueError("naming stage identity changed")
    cap = LOCAL_OUTPUT_TOKENS if job["stage"] == "local" else RECONCILIATION_OUTPUT_TOKENS
    if (payload.get("max_completion_tokens") != cap or job.get("output_token_limit") != cap
            or job.get("request_digest") != _digest(job.get("identity"))
            or job["identity"].get("request_payload") != payload):
        raise ValueError("naming stage bounds or exact identity changed")
    upper = len(_json(payload).encode("utf-8")) + 2048
    if upper > MAX_INPUT_TOKENS or job.get("input_token_upper_bound") != upper:
        raise ValueError("naming stage input bound changed")
    return upper, cap


def _reconciliation_bound(preview):
    # These are valid local placeholders for sizing only, never provider inputs
    # or evidence. Four bytes per allowed title/scope character covers escaped
    # JSON punctuation and Unicode without assuming the eventual model output.
    drafts = {cid: {"community_id": cid, "title": f"Pending community {at}", "scope": "Pending scope."}
              for at, cid in enumerate(preview["local_jobs"])}
    job = finish_reconciliation(preview, drafts)
    upper, output = _job_bounds(job)
    upper += len(drafts) * 4 * (100 + 320)
    if upper > MAX_INPUT_TOKENS:
        raise ValueError("reconciliation cannot fit its bounded context; no community was dropped")
    return upper, output


def _rates(input_price, output_price, identity):
    if input_price is None or output_price is None:
        return None
    if not isinstance(identity, str) or not identity.strip() or len(identity) > 500:
        raise ValueError("explicit naming pricing identity is required")
    return {"input_per_million": str(_money(input_price, "input price")),
            "output_per_million": str(_money(output_price, "output price")), "identity": identity}


def _cost(bounds, rates):
    return ((Decimal(bounds[0]) * Decimal(rates["input_per_million"])
             + Decimal(bounds[1]) * Decimal(rates["output_per_million"])) / Decimal(1_000_000))


class _NamingSpendLedger:
    """Atomic admission shared by every stage/request under one approval ID."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS naming_budgets (approval_id TEXT PRIMARY KEY, ceiling TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS naming_calls (digest TEXT PRIMARY KEY, approval_id TEXT NOT NULL, reserved TEXT NOT NULL, status TEXT NOT NULL, result TEXT, checksum TEXT)")
        os.chmod(self.path, 0o600)

    def _db(self):
        if self.path.is_symlink():
            raise ValueError("naming ledger cannot be a symbolic link")
        if self.path.exists():
            info = self.path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("naming ledger must be a dedicated regular file")
        return sqlite3.connect(self.path, timeout=30)

    def reserve(self, digest, approval_id, ceiling, reserve):
        import json
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT ceiling FROM naming_budgets WHERE approval_id=?", (approval_id,)).fetchone()
            if prior and Decimal(prior[0]) != ceiling:
                raise ValueError("naming approval ceiling cannot be changed")
            db.execute("INSERT OR IGNORE INTO naming_budgets VALUES (?,?)", (approval_id, str(ceiling)))
            old = db.execute("SELECT status,result,checksum FROM naming_calls WHERE digest=?", (digest,)).fetchone()
            if old:
                if old[0] != "complete":
                    raise ValueError("naming outcome is unknown; no automatic retry")
                value = json.loads(old[1])
                if old[2] != _digest({"digest": digest, "result": value}):
                    raise ValueError("naming result ledger checksum mismatch")
                return value
            rows = db.execute("SELECT reserved,status FROM naming_calls WHERE approval_id=?", (approval_id,)).fetchall()
            if any(row[1] != "complete" for row in rows):
                raise ValueError("an unknown naming outcome holds this approval")
            if sum((Decimal(row[0]) for row in rows), Decimal(0)) + reserve > ceiling:
                raise ValueError("aggregate naming budget exhausted")
            # Unknown is durable before opening a provider. Completed calls keep
            # their conservative reservation; no usage-derived refund is guessed.
            db.execute("INSERT INTO naming_calls VALUES (?,?,?,'unknown',NULL,NULL)",
                       (digest, approval_id, str(reserve)))
        return None

    def complete(self, digest, result):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT status FROM naming_calls WHERE digest=?", (digest,)).fetchone()
            if old != ("unknown",):
                raise ValueError("naming request is not awaiting settlement")
            db.execute("UPDATE naming_calls SET status='complete',result=?,checksum=? WHERE digest=?",
                       (_json(result), _digest({"digest": digest, "result": result}), digest))


def run_two_stage_naming(
    preview, cache, *, reader=None, enabled=False, approval=None,
    input_price_per_million=None, output_price_per_million=None, pricing_identity="",
    ledger_path=None,
):
    """Reuse results, preview missing work, or execute under separate approval.

    An approval contains approved=True, budget_scope='community_naming',
    approval_id, request_digest from this inert preview and max_spend_usd. The
    digest binds evidence, provider, model, template and supplied prices; cache
    progress alone does not change it. No map or note is written here.
    """
    if type(enabled) is not bool:
        raise ValueError("enabled must be a boolean")
    preview = deepcopy(preview)
    _verify_preview(preview)
    replay = cache.replay(preview)
    if replay["status"] == "cached":
        return {**replay, "additional_cost_usd": "0", "paid_dispatch_enabled": False}
    rates = _rates(input_price_per_million, output_price_per_million, pricing_identity)
    missing_jobs = {cid: preview["local_jobs"][cid] for cid in replay["missing_community_ids"]}
    bounds = [_job_bounds(job) for job in missing_jobs.values()]
    bounds.append(_job_bounds(replay["reconciliation_preview"]) if not missing_jobs else _reconciliation_bound(preview))
    digest = _digest({"run_version": RUN_VERSION, "template_version": VERSION,
                      "evidence_identity": preview["evidence_identity"], "provider": preview["provider"],
                      "model": preview["model"], "pricing": rates,
                      "reconciliation_template": preview["reconciliation_template"]})
    reserve = sum((_cost(bound, rates) for bound in bounds), Decimal(0)) if rates else None
    inert = {**replay, "status": "preview", "request_digest": digest,
             "preview": preview, "missing_local_jobs": missing_jobs,
             "paid_dispatch_enabled": False, "pricing": rates,
             "reserved_cost_usd": str(reserve) if reserve is not None else None,
             "additional_cost_usd": "0", "missing_request_count": len(missing_jobs) + 1,
             "reconciliation_input_upper_bound": bounds[-1][0], "actual_cost_usd": None}
    if not enabled:
        return inert
    if rates is None:
        raise ValueError("naming dispatch needs explicit verified pricing")
    if (not isinstance(approval, Mapping) or approval.get("approved") is not True
            or approval.get("budget_scope") != "community_naming"
            or approval.get("request_digest") != digest):
        raise ValueError("separate exact two-stage naming approval required")
    approval_id = approval.get("approval_id")
    if not isinstance(approval_id, str) or not approval_id.strip() or len(approval_id) > 200:
        raise ValueError("naming approval ID is required")
    ceiling = _money(approval.get("max_spend_usd"), "naming ceiling")
    if reserve > ceiling:
        raise ValueError("missing naming work exceeds its approved budget")
    if ((getattr(reader, "name", None), getattr(reader, "model", None))
            != (preview["provider"], preview["model"])
            or not callable(getattr(reader, "name_community_stage", None))):
        raise ValueError("two-stage naming reader differs from the pinned provider/model")
    ledger = _NamingSpendLedger(ledger_path or cache.path.with_name(cache.path.name + ".spending.sqlite3"))
    drafts = deepcopy(replay["drafts"])
    calls, reserved = 0, Decimal(0)

    def execute(job, *, cid=None):
        nonlocal calls, reserved
        amount = _cost(_job_bounds(job), rates)
        result = ledger.reserve(job["request_digest"], approval_id, ceiling, amount)
        if result is None:
            payload = job["request_payload"]
            raw = reader.name_community_stage(payload["messages"][0]["content"], payload["messages"][1]["content"],
                                              stage=job["stage"], output_tokens=job["output_token_limit"])
            result = validate_local_result(raw, cid) if cid else validate_final_result(raw, preview)
            ledger.complete(job["request_digest"], result)
            calls += 1
            reserved += amount
        else:
            result = validate_local_result(result, cid) if cid else validate_final_result(result, preview)
        provenance = {"origin": "generated", "provider": preview["provider"], "model": preview["model"],
                      "template_version": VERSION, "request_digest": job["request_digest"],
                      "request_payload_sha256": _digest(job["request_payload"]), "response_sha256": _digest(result)}
        if cid:
            cache.put_local(preview, cid, result, provenance)
        else:
            cache.put_final(preview, drafts, result, provenance)
        return result

    for cid, job in missing_jobs.items():
        drafts[cid] = execute(job, cid=cid)
    reconciliation = finish_reconciliation(preview, drafts)
    _job_bounds(reconciliation)
    if reconciliation["input_token_upper_bound"] > inert["reconciliation_input_upper_bound"]:
        raise ValueError("reconciliation exceeds the separately previewed stage bound")
    execute(reconciliation)
    return {**cache.replay(preview), "status": "generated" if calls else "cached", "provider_calls": calls,
            "reserved_cost_usd": str(reserved), "actual_cost_usd": None,
            "additional_cost_usd": None if calls else "0", "paid_dispatch_enabled": False,
            "request_digest": digest}
