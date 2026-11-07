"""Builder: writes (and repairs) the agent code, using the CURRENT skill version as its
engineering knowledge. The baseline gets only the locked hosted contract."""
from __future__ import annotations

import json

from .. import llm
from . import recovery

ROLE = """You are the Builder in FOUNDRY. You write complete, working Python for Fetch.ai agents.
Follow the engineering lessons below exactly; they were learned from real failures.

{lessons}

Architect's plan (advisory):
{plan}
"""


def _messages(request, plan, skills):
    return [{"role": "system", "content": ROLE.format(lessons=skills.prompt(), plan=json.dumps(plan, indent=1))},
            {"role": "user", "content": f"Build this:\n\n{request}\n\nReturn every agent's full code."}]


def build(request: str, plan: dict, skills, meter, events=None) -> dict:
    def go(provider):
        r = llm.chat(_messages(request, plan, skills), provider=provider, role="builder", meter=meter, max_tokens=8000)
        out = llm.extract_agents(r["text"])
        out["provider"] = r["provider"]
        return out
    return recovery.call(go, provider=skills.route("builder"), events=events)


def repair(request: str, plan: dict, prev: dict, feedback: str, skills, meter, events=None) -> dict:
    code = "\n\n".join(f"```python name={a['name']}{' entry' if a['name'] == prev['entry'] else ''}\n{a['code']}```"
                       for a in prev["agents"])
    msgs = _messages(request, plan, skills) + [
        {"role": "assistant", "content": code},
        {"role": "user", "content": "Independent verification FAILED. You do not see the tests, only what failed:\n\n"
                                    f"{feedback}\n\nFind the root cause, fix it, and return the COMPLETE corrected "
                                    "code for every agent in the same block format."},
    ]

    def go(provider):
        r = llm.chat(msgs, provider=provider, role="repair", meter=meter, max_tokens=8000)
        out = llm.extract_agents(r["text"])
        out["provider"] = r["provider"]
        return out
    return recovery.call(go, provider=skills.route("repair"), events=events)
