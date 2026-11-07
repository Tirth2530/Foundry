"""Operational usage ledger (SQLite): one row per REAL model call, written exactly once.

Adapted from Dapta's UsageLedger (backend/dapta/ledger.py, MIT, Copyright (c) 2026 Aditya Venkat; see
THIRD_PARTY_NOTICES.md): idempotent inserts keyed by call id (a retry of the same record is a no-op, a
conflicting record is an error), integer nano-dollar money columns, explicit cost sources, and a ban on
prompt/response content or credentials in stored metadata.

Division of authority (so there are never two sources of truth for the same fact):
  usage.db             model-call ACCOUNTING: tokens, estimated / calculated / provider-reported cost.
                       Authoritative for spend ceilings across restarts.
  evidence/ledger.jsonl  DECISIONS and EVENTS: builds, verdicts, patches, promotions, publishes, payments.
                       Hash-chained (tamper-EVIDENT for edits to earlier lines, not tamper-proof: anyone who
                       can rewrite the whole file can recompute the chain).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from . import config

PATH = config.EVIDENCE / "usage.db"
NANOS = Decimal("1000000000")
COST_SOURCES = {"provider_reported", "calculated", "estimated", "none"}
FORBIDDEN_METADATA_KEYS = {"api_key", "authorization", "content", "messages", "password", "prompt",
                           "request_body", "response_body", "response_text", "secret", "token", "text"}
_lock = threading.Lock()


class UsageConflict(ValueError):
    pass


def _nanos(usd) -> int | None:
    if usd is None:
        return None
    d = Decimal(str(usd))
    if not d.is_finite() or d < 0:
        raise ValueError(f"invalid USD amount {usd!r}")
    return int((d * NANOS).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _usd(nanos) -> float | None:
    return None if nanos is None else float(Decimal(nanos) / NANOS)


def _check_metadata(value) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            if str(k).lower() in FORBIDDEN_METADATA_KEYS or str(k).lower().endswith("_content"):
                raise ValueError(f"sensitive metadata field is not allowed in the usage ledger: {k}")
            _check_metadata(v)
    elif isinstance(value, list):
        for v in value:
            _check_metadata(v)


@contextmanager
def _connect():
    PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 30000")
    try:
        con.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        pass
    try:
        con.execute("""CREATE TABLE IF NOT EXISTS model_calls (
            call_id TEXT PRIMARY KEY, job_id TEXT, run_id TEXT, role TEXT NOT NULL, provider TEXT NOT NULL,
            model TEXT NOT NULL, tokens_in INTEGER NOT NULL, tokens_out INTEGER NOT NULL, usage_source TEXT NOT NULL,
            estimated_nanos INTEGER, calculated_nanos INTEGER, provider_nanos INTEGER, charged_nanos INTEGER NOT NULL,
            cost_source TEXT NOT NULL, fell_back INTEGER NOT NULL, latency_ms INTEGER, t REAL,
            metadata_json TEXT NOT NULL)""")
        con.execute("CREATE INDEX IF NOT EXISTS calls_job ON model_calls(job_id)")
        con.execute("CREATE INDEX IF NOT EXISTS calls_run ON model_calls(run_id)")
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def record(rec: dict) -> str:
    """Insert one model call. Idempotent on call_id; a DIFFERENT record under the same id raises."""
    meta = rec.get("metadata") or {}
    _check_metadata(meta)
    if rec.get("cost_source", "none") not in COST_SOURCES:
        raise ValueError(f"invalid cost source {rec.get('cost_source')}")
    row = {"call_id": rec["call_id"], "job_id": rec.get("job_id"), "run_id": rec.get("run_id"), "role": rec["role"],
           "provider": rec["provider"], "model": rec["model"], "tokens_in": int(rec["tokens_in"]),
           "tokens_out": int(rec["tokens_out"]), "usage_source": rec.get("usage_source", "provider"),
           "estimated_nanos": _nanos(rec.get("estimated_usd")), "calculated_nanos": _nanos(rec.get("calculated_usd")),
           "provider_nanos": _nanos(rec.get("provider_usd")), "charged_nanos": _nanos(rec["usd"]),
           "cost_source": rec.get("cost_source", "calculated"), "fell_back": int(bool(rec.get("fell_back"))),
           "latency_ms": rec.get("latency_ms"), "t": None if rec.get("t") is None else float(rec["t"]),
           "metadata_json": json.dumps(meta, sort_keys=True, separators=(",", ":"))}
    cols = tuple(row)
    with _lock:
        try:
            with _connect() as con:
                con.execute(f"INSERT INTO model_calls ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                            tuple(row[c] for c in cols))
        except sqlite3.IntegrityError as e:
            with _connect() as con:
                old = con.execute(f"SELECT {','.join(cols)} FROM model_calls WHERE call_id=?",
                                  (row["call_id"],)).fetchone()
            if old is not None and all(old[c] == row[c] for c in cols):
                return row["call_id"]  # exact replay: idempotent no-op
            raise UsageConflict(f"conflicting usage record for call {row['call_id']}") from e
    return row["call_id"]


def total_usd(*, provider: str | None = None, job_id: str | None = None, run_id: str | None = None) -> float:
    q, args = "SELECT COALESCE(SUM(charged_nanos), 0) AS n FROM model_calls WHERE 1=1", []
    for col, val in (("provider", provider), ("job_id", job_id), ("run_id", run_id)):
        if val is not None:
            q += f" AND {col}=?"
            args.append(val)
    if not PATH.exists():
        return 0.0
    with _connect() as con:
        return _usd(con.execute(q, args).fetchone()["n"]) or 0.0


def summary(*, job_id: str | None = None, run_id: str | None = None) -> dict:
    """Job- or run-level summary with the three cost kinds kept separate (never blended)."""
    if not PATH.exists():
        return {"calls": 0}
    where, args = ("job_id=?", [job_id]) if job_id else (("run_id=?", [run_id]) if run_id else ("1=1", []))
    with _connect() as con:
        r = con.execute(f"""SELECT COUNT(*) AS calls, SUM(tokens_in) AS tin, SUM(tokens_out) AS tout,
            SUM(estimated_nanos) AS est, SUM(calculated_nanos) AS calc, SUM(provider_nanos) AS prov,
            SUM(charged_nanos) AS charged, SUM(CASE WHEN provider_nanos IS NOT NULL THEN 1 ELSE 0 END) AS prov_n,
            SUM(CASE WHEN usage_source != 'provider' THEN 1 ELSE 0 END) AS approx_n
            FROM model_calls WHERE {where}""", args).fetchone()
        by = con.execute(f"""SELECT role, provider, COUNT(*) AS n, SUM(charged_nanos) AS c FROM model_calls
            WHERE {where} GROUP BY role, provider""", args).fetchall()
    return {"calls": r["calls"], "tokens_in": r["tin"] or 0, "tokens_out": r["tout"] or 0,
            "estimated_usd": _usd(r["est"]), "calculated_usd": _usd(r["calc"]),
            "provider_reported_usd": _usd(r["prov"]) if r["prov_n"] else None,
            "provider_reported_calls": r["prov_n"] or 0, "charged_usd": _usd(r["charged"]) or 0.0,
            "calls_without_provider_usage": r["approx_n"] or 0,
            "by_role": {f"{b['role']}/{b['provider']}": {"calls": b["n"], "usd": _usd(b["c"])} for b in by}}
