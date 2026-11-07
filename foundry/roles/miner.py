"""Failure Miner: clusters failures by code (deterministic), then asks a model for the
single generalizable root cause of the biggest cluster."""
from __future__ import annotations

from collections import Counter

from .. import llm
from . import recovery

SYSTEM = """You analyse failures of an AI that builds Fetch.ai chat agents. Given a failure cluster and
examples, state the ONE root cause in the builder's engineering knowledge that would explain them across
DIFFERENT tasks. Be concrete about the API detail involved. Never mention specific task names or test
values. Return JSON only: {"root_cause": "...", "generalizes_because": "..."}"""


def mine(builds: list[dict], skills, meter, exclude_codes: set | None = None, events=None) -> dict | None:
    exclude_codes = exclude_codes or set()
    fails = []
    for b in builds:
        for f in (b.get("hidden") or {}).get("failures", []):
            fails.append({**f, "spec": b.get("spec_id"), "job": b.get("job_id")})
        if b.get("status") == "error":
            fails.append({"code": "BUILD_ERROR", "detail": b.get("error", ""), "spec": b.get("spec_id"),
                          "job": b.get("job_id"), "case": "-", "send": ""})
    counts = Counter(f["code"] for f in fails if f["code"] not in exclude_codes)
    if not counts:
        return None
    code, n = counts.most_common(1)[0]
    specs = sorted({f["spec"] for f in fails if f["code"] == code})
    examples = [f for f in fails if f["code"] == code][:8]
    cluster = {"code": code, "count": n, "specs": specs, "spec_count": len(specs),
               "all_codes": dict(counts), "examples": examples}
    ex = "\n".join(f"- [{e['code']}] sent {e.get('send', '')!r}: {e['detail'][:300]}" for e in examples)

    def go(p):
        r = llm.chat([{"role": "system", "content": SYSTEM},
                      {"role": "user", "content": f"Cluster {code} ({n} failures across {len(specs)} tasks):\n{ex}"}],
                     provider=p, role="miner", meter=meter, max_tokens=600)
        return llm.extract_json(r["text"])
    cluster.update(recovery.call(go, provider=skills.route("miner"), events=events))
    return cluster
