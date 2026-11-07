"""Append-only evidence ledger (JSONL). Every build, patch proposal, promotion, rejection,
rollback, publish and payment is one line. Records are hash-chained so edits are detectable."""
from __future__ import annotations

import hashlib
import json
import threading
import time

from . import config

PATH = config.EVIDENCE / "ledger.jsonl"
_lock = threading.Lock()


def _last_hash() -> str:
    if not PATH.exists():
        return "genesis"
    last = ""
    with PATH.open() as f:
        for line in f:
            if line.strip():
                last = line
    return json.loads(last)["hash"] if last else "genesis"


def append(kind: str, **data) -> dict:
    """Thread-safe AND process-safe (serve + evolve can run at the same time): an exclusive file lock
    covers reading the last hash and writing the new record, so the chain can't fork."""
    with _lock:
        config.EVIDENCE.mkdir(parents=True, exist_ok=True)
        with (config.EVIDENCE / ".ledger.lock").open("a") as lockf:
            try:
                import fcntl
                fcntl.flock(lockf, fcntl.LOCK_EX)
            except ImportError:  # Windows: thread lock only
                pass
            rec = {"kind": kind, "t": time.strftime("%Y-%m-%dT%H:%M:%S"), **data, "prev": _last_hash()}
            rec["hash"] = hashlib.sha256(json.dumps(rec, sort_keys=True, default=str).encode()).hexdigest()[:16]
            with PATH.open("a") as f:
                f.write(json.dumps(rec, default=str) + "\n")
            return rec


def read(kind: str | None = None) -> list[dict]:
    if not PATH.exists():
        return []
    out = [json.loads(l) for l in PATH.read_text().splitlines() if l.strip()]
    return [r for r in out if kind is None or r["kind"] == kind]


def verify_chain() -> tuple[bool, int]:
    prev = "genesis"
    recs = read()
    for i, r in enumerate(recs):
        body = {k: v for k, v in r.items() if k != "hash"}
        h = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:16]
        if r.get("prev") != prev or h != r.get("hash"):
            return False, i
        prev = r["hash"]
    return True, len(recs)
