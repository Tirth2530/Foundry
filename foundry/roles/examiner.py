"""Spec Examiner: writes acceptance tests from the REQUEST ONLY. It never sees the code, and
by default runs on a different model family (Meta Llama) than the Builder (GPT), so the
verifier does not share the implementer's blind spots. Tests are cached per request."""
from __future__ import annotations

import hashlib
import json

from .. import config, dsl, llm
from . import recovery

CACHE = config.RUNS / "_examiner_cache"

SYSTEM = """You are an independent test engineer. You receive ONLY a product request for a chat agent
(you will never see its code). Write 4-6 acceptance test cases a correct implementation must pass.

Each case: {"name": str, "turns": [{"send": "<message the user sends>", "checks": [<check>...]}]}
Checks:
 {"type":"ack"}                                   message was acknowledged
 {"type":"text_contains","value":"X"}             reply contains X (case-insensitive); or "any_of":[...]
 {"type":"text_not_contains","value":"X"}
 {"type":"text_regex","pattern":"..."}
 {"type":"text_number","value":12.5,"tol":0.01}   reply contains this number
 {"type":"card","kind":"carousel|detail|form|review","min_items":1,"fields":["name"]}  a valid card is attached
 {"type":"no_card"}
 {"type":"end_session"}
Rules: assert ONLY what the request states explicitly (exact keywords/formats it mandates). Compute expected
values carefully. Use multi-turn cases for memory/state. When the request describes card buttons, send the
button's selection as a JSON string, e.g. {"action":"approve"}. Always include an ack check in the first turn.
Return JSON only: {"cases":[...]}"""


def write_tests(request: str, skills, meter, events=None) -> dict:
    provider = skills.route("examiner")
    key = hashlib.sha256(f"{provider}|{request}".encode()).hexdigest()[:16]
    CACHE.mkdir(parents=True, exist_ok=True)
    cached = CACHE / f"{key}.json"
    if cached.exists():
        return json.loads(cached.read_text())

    def go(p):
        r = llm.chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": request}],
                     provider=p, role="examiner", meter=meter, max_tokens=8000)
        data = llm.extract_json(r["text"])
        raw = data.get("cases", []) if isinstance(data, dict) else data
        good, dropped = [], []
        for i, c in enumerate(raw):
            if isinstance(c, dict):
                c.setdefault("name", f"exam_{i}")
            errs = dsl.validate_case(c)
            (dropped if errs else good).append(c if not errs else {"case": c, "errors": errs})
        if not good:
            raise ValueError("examiner produced no valid cases")
        return {"cases": good, "dropped": dropped, "provider": r["provider"],
                "independent": r["provider"] != skills.route("builder")}
    out = recovery.call(go, provider=provider, events=events, avoid=skills.route("builder"))
    cached.write_text(json.dumps(out, indent=1))
    return out
