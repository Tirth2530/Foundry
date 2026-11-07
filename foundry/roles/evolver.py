"""Skill Evolver (Lesson Writer): proposes the SMALLEST change to FOUNDRY's own engineering
process that addresses the mined root cause. It edits skills (lessons), tools, routing or
the repair budget. It never sees the promotion or final tests."""
from __future__ import annotations

import json

from .. import llm, skills as sk
from . import recovery

SYSTEM = """You improve FOUNDRY, an AI system that builds Fetch.ai agents. You change FOUNDRY ITSELF, not
any one agent. Propose exactly ONE patch, the smallest that fixes the root cause across different tasks.

Patch kinds (JSON only):
 {"kind":"lesson_add","file":"NNN-short-name.md","content":"<=1800 chars of precise, reusable guidance, ideally a tiny code snippet"}
 {"kind":"lesson_edit","file":"<existing file>","content":"<full new content>"}
 {"kind":"lesson_remove","file":"<existing file>"}           (remove a lesson that is wrong or wasteful)
 {"kind":"config","config_changes":{"tools":{"selection_json_probe":true}}}
     tools: selection_json_probe, include_reply_text  (the static gate and independent tests are locked on)
     also: {"repair_budget": 0-4} or {"routing": {"<role>": "gpt|meta|asi"}}
Add "rationale" and "targets" (the failure code) to the JSON.
Rules: lessons must generalize; never mention task names, test inputs or expected outputs; prefer exact API
facts (field names, types, call shapes) learned from the error messages; 000-* files are locked; do not
repeat a previously rejected idea."""


def propose(cluster: dict, skills, meter, history: list[dict], events=None) -> dict:
    lessons = "\n\n".join(f"### {n}\n{t}" for n, t in skills.lessons)
    rejected = [h for h in history if h.get("decision") == "rejected"][-6:]
    ctx = (f"Current lessons:\n{lessons}\n\nCurrent config:\n{json.dumps(skills.config)}\n\n"
           f"Root cause to fix ({cluster['code']}, {cluster['count']} failures over {cluster['spec_count']} tasks):\n"
           f"{cluster.get('root_cause')}\nWhy it generalizes: {cluster.get('generalizes_because')}\n\n"
           "Representative error messages:\n" +
           "\n".join(f"- {e['detail'][:260]}" for e in cluster["examples"][:6]) +
           "\n\nPreviously rejected patches (do not repeat):\n" +
           "\n".join(f"- {h.get('patch', {}).get('kind')} {h.get('patch', {}).get('file', '')}: {h.get('reason')}" for h in rejected))
    msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": ctx}]

    def go(p):
        r = llm.chat(msgs, provider=p, role="evolver", meter=meter, max_tokens=1500)
        patch = llm.extract_json(r["text"])
        errs = sk.validate_patch(patch, skills)
        if errs:
            msgs.append({"role": "assistant", "content": r["text"]})
            msgs.append({"role": "user", "content": "Invalid patch: " + "; ".join(errs) + ". Return a corrected patch."})
            raise ValueError("; ".join(errs))
        return patch
    return recovery.call(go, provider=skills.route("evolver"), events=events)
