"""End-to-end job traces: runs/<job>/trace.jsonl, one span per line.

  ASI:One intent -> Foreman -> architect / examiner / builder (role agents) -> sandbox runner -> card validator
  -> release gate -> publisher (create, upload, start, live call, discovery) -> live result

Every span carries the job id as trace id, a span id, its parent, timing, ok/error and a STABLE error category.
Attributes are redacted: configured credentials and secret-looking keys never reach the file. Error
categorisation and redaction are adapted from Dapta's observability module (backend/dapta/observability.py, MIT,
Copyright (c) 2026 Aditya Venkat). Sentry is not required; this file is the inspectable artifact.
"""
from __future__ import annotations

import contextvars
import json
import re
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import config

SECRET_KEYS = re.compile(r"(api[_-]?key|authorization|secret|token|password|seed|client_secret)", re.I)
_current: contextvars.ContextVar = contextvars.ContextVar("foundry_trace", default=None)
_lock = threading.Lock()


def _secrets() -> list[str]:
    vals = [config.AGENTVERSE_API_KEY, config.STRIPE_SECRET_KEY, config.FOREMAN_SEED, config.env("GITHUB_TOKEN"),
            config.env("CLAIM_AGENTVERSE_API_KEY")] + [p.get("api_key") for p in config.PROVIDERS.values()]
    return [v for v in vals if v and len(str(v)) >= 6]


def redact(obj, _secrets_cache=None):
    secrets = _secrets_cache if _secrets_cache is not None else _secrets()
    if isinstance(obj, dict):
        return {k: ("[REDACTED]" if SECRET_KEYS.search(str(k)) else redact(v, secrets)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact(v, secrets) for v in obj]
    if isinstance(obj, str):
        for s in secrets:
            obj = obj.replace(s, "[REDACTED]")
        return obj
    return obj


def error_category(e: BaseException) -> str:
    """Stable, content-free failure class (for dashboards and for the Failure Miner)."""
    name, text = type(e).__name__, str(e).lower()
    status = getattr(e, "status", None)
    if name in ("BudgetExceeded", "PricingUnknown"):
        return "budget"
    if name == "Cancelled":
        return "cancelled"
    if name in ("Unauthorized", "PermissionError"):
        return "auth"
    if name in ("InvalidTransition", "PublishConflict"):
        return "conflict"
    if isinstance(e, TimeoutError) or "timed out" in text or status in (408, 504):
        return "timeout"
    if status == 429 or "http 429" in text:
        return "rate_limited"
    if status in (401, 403) or "http 401" in text or "http 403" in text:
        return "auth"
    if isinstance(status, int) and status >= 500 or re.search(r"http 5\d\d", text):
        return "provider_error"
    if isinstance(status, int) and status >= 400 or re.search(r"http 4\d\d", text):
        return "bad_request"
    if name in ("ConnectionError", "RequestException") or "connection" in text:
        return "network"
    if name in ("JSONDecodeError", "LLMError") and "json" in text:
        return "invalid_response"
    if isinstance(e, ValueError):
        return "validation"
    return "internal"


class Tracer:
    def __init__(self, job_id: str, path: Path | None = None):
        self.trace_id = job_id
        self.path = path or (config.RUNS / job_id / "trace.jsonl")
        self.stack: list[str] = []
        self.secrets = _secrets()

    def _write(self, rec: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _lock, self.path.open("a") as f:
            f.write(json.dumps(redact(rec, self.secrets), default=str) + "\n")

    def event(self, name: str, **attrs) -> None:
        self._write({"trace_id": self.trace_id, "span_id": uuid.uuid4().hex[:12], "parent": self.stack[-1]
                     if self.stack else None, "name": name, "t": time.time(), "event": True, "attrs": attrs})

    @contextmanager
    def span(self, name: str, **attrs):
        sid = uuid.uuid4().hex[:12]
        parent = self.stack[-1] if self.stack else None
        self.stack.append(sid)
        t0 = time.time()
        rec = {"trace_id": self.trace_id, "span_id": sid, "parent": parent, "name": name, "start": t0, "attrs": attrs}
        try:
            yield rec["attrs"]
            rec["ok"] = True
        except BaseException as e:
            rec.update(ok=False, error_category=error_category(e), error=f"{type(e).__name__}: {str(e)[:300]}")
            raise
        finally:
            self.stack.pop()
            rec["ms"] = int(1000 * (time.time() - t0))
            self._write(rec)


def current() -> Tracer | None:
    return _current.get()


@contextmanager
def activate(tracer: Tracer):
    token = _current.set(tracer)
    try:
        yield tracer
    finally:
        _current.reset(token)


@contextmanager
def span(name: str, **attrs):
    """Span on the active tracer, or a no-op when none is active."""
    t = current()
    if t is None:
        yield attrs
        return
    with t.span(name, **attrs) as a:
        yield a


def read(job_id: str, job_dir: Path | None = None) -> list[dict]:
    p = (job_dir or (config.RUNS / job_id)) / "trace.jsonl"
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()] if p.exists() else []


def render(spans: list[dict]) -> str:
    """Indented tree, in start order."""
    kids: dict = {}
    for s in spans:
        kids.setdefault(s.get("parent"), []).append(s)
    lines = []

    def walk(parent, depth):
        for s in sorted(kids.get(parent, []), key=lambda x: x.get("start") or x.get("t") or 0):
            status = "event" if s.get("event") else ("ok" if s.get("ok") else f"FAIL[{s.get('error_category')}]")
            attrs = {k: v for k, v in (s.get("attrs") or {}).items() if v not in (None, "", [], {})}
            lines.append(f"{'  ' * depth}{s['name']:<28} {status:<18} {str(s.get('ms', '')) + 'ms' if 'ms' in s else '':>8}"
                         f"  {json.dumps(attrs, default=str)[:140]}")
            walk(s["span_id"], depth + 1)
    walk(None, 0)
    return "\n".join(lines)
