"""Adversarial Tester: robustness probes. A production agent must acknowledge and answer
gracefully (no crash, no silence) on empty, oversized, junk and malformed-selection input."""
from __future__ import annotations

from .. import llm
from . import recovery

BASE = [
    {"name": "adv_empty", "turns": [{"send": " ", "checks": [{"type": "ack"}]}]},
    {"name": "adv_oversized", "turns": [{"send": "x" * 4000, "checks": [{"type": "ack"}]}]},
    {"name": "adv_junk", "turns": [{"send": "}{ ;; DROP TABLE users; -- ‮\u0000 🙃", "checks": [{"type": "ack"}]}]},
]
SELECTION_PROBE = {"name": "adv_selection_json", "turns": [
    {"send": '{"action": "unknown_action", "id": "zzz"}', "checks": [{"type": "ack"}]}]}

SYSTEM = """You are a hostile QA tester for a chat agent. Given its request, propose 2 tricky but plausible
user messages that commonly crash naive implementations (edge values, missing parts, odd spacing/casing).
Return JSON only: {"messages": ["...", "..."]}"""


def cases(request: str, skills, meter, events=None) -> list[dict]:
    out = [dict(c) for c in BASE]
    if skills.tool("selection_json_probe"):
        out.append(SELECTION_PROBE)
    try:
        def go(p):
            r = llm.chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": request}],
                         provider=p, role="adversary", meter=meter, max_tokens=3000)
            return llm.extract_json(r["text"]).get("messages", [])[:2]
        for i, m in enumerate(recovery.call(go, provider=skills.route("adversary"), attempts=1, events=events,
                                                avoid=skills.route("builder"))):
            if isinstance(m, str) and m.strip():
                out.append({"name": f"adv_llm_{i}", "turns": [{"send": m[:1000], "checks": [{"type": "ack"}]}]})
    except llm.BudgetExceeded:
        raise
    except Exception as e:  # noqa: BLE001 - adversary is best-effort
        if events is not None:
            events.append({"adversary": "llm probes skipped", "error": str(e)[:200]})
    return out
