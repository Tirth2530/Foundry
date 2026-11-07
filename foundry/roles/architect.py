"""Architect: turns a request into a small, explicit system plan (agents, messages, state, cards)."""
from __future__ import annotations

from .. import llm
from . import recovery

SYSTEM = """You are the Architect in FOUNDRY, a system that builds Fetch.ai multi-agent software.
Design the SMALLEST system that satisfies the request. Return JSON only:
{"agents":[{"name":"snake_case","role":"one line"}],"entry":"name of the agent users talk to",
 "inter_agent_messages":["who sends what to whom"],"state":"what per-user state is kept, or none",
 "cards":"which interactive card kinds are needed and when, or none",
 "risks":["the 2-3 details most likely to break"]}
Use exactly as many agents as the request asks for (default 1)."""


def plan(request: str, skills, meter, events=None) -> dict:
    def go(provider):
        r = llm.chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": request}],
                     provider=provider, role="architect", meter=meter, max_tokens=4000)
        p = llm.extract_json(r["text"])
        if not isinstance(p, dict) or not p.get("agents"):
            raise ValueError("plan has no agents")
        p["_provider"] = r["provider"]
        return p
    return recovery.call(go, provider=skills.route("architect"), events=events)
