"""Concierge: the conversational layer between a person in ASI:One and FOUNDRY's build machinery.

It talks like an engineer would: understands what the person is after, asks the one or two questions that
actually change the design, proposes a concrete BUILD PLAN (shown as a card the person confirms), and answers
questions about FOUNDRY, its evidence and its self-improvement. It never spends money or starts work by itself:
searching, quoting, building and publishing only happen after an explicit tap on a card.

The model returns strict JSON; anything malformed falls back to deterministic behaviour, so a model hiccup can
never strand the user or start a build.
"""
from __future__ import annotations

import json
import re

from .. import config, llm, skills as sk

HISTORY_TURNS = 16  # messages of context kept per user

SYSTEM = """You are FOUNDRY's concierge inside ASI:One (Fetch.ai). You are talking with a person who may want an
agent built, may want to find an existing one, or may just be curious about FOUNDRY. Be a sharp, warm senior
engineer: concise (usually 2-5 sentences), concrete, no hype, no filler, no emoji spam. Use **bold** sparingly.

WHAT FOUNDRY IS (facts you may rely on):
- It builds Fetch.ai uAgents (single agents or multi-agent systems) that speak the Agent Chat Protocol and can
  reply with ASI:One interactive cards (forms, review/approve cards, carousels, detail cards).
- Pipeline once the user confirms a plan: (1) the Scout searches Agentverse + FOUNDRY's verified catalog and
  RE-TESTS the best existing agents live (a README or a search rank is not proof) and offers reuse if one really
  works; (2) the Quartermaster quotes cost from measured history; (3) an independent Examiner (Meta Muse, a
  different model family that never sees the code) writes hidden tests, plus an Adversary writes edge cases;
  (4) GPT-5 builds; the code runs in a sandbox over the real chat protocol; failures go back for repair;
  (5) a release gate: all tests pass, static safety checks, valid cards, and for multi-agent systems observed
  real messages between the agents; (6) a Proof Passport with hashes of code + evidence; (7) one tap publishes
  to Agentverse, where it is live-tested again and checked for discoverability.
- It gets better over time: defects caught in builds become candidate skill changes; a sealed Promotion Judge
  accepts a change only if it solves more held-out tasks with zero regressions (or is >=15% cheaper); every
  decision is in a hash-chained ledger and can be rolled back. Current skill version: {version}.
- What generated agents CAN do: self-contained logic in Python stdlib (parsing, validation, conversion,
  calculations, routing, workflows, approvals, per-user memory via agent storage, multi-agent coordination).
  What they should NOT be promised: calling external paid APIs with secrets, scraping websites, live market
  data, or anything needing credentials. If the person wants that, say so honestly and offer the closest
  buildable version (e.g. the agent validates/normalises/plans while the person supplies the data).

HOW TO RESPOND - pick exactly one intent:
- "greet": first hello / "what can you do". Introduce yourself in 2-3 sentences, ask what they want.
- "chat": answer a question or discuss. No build.
- "clarify": a build is wanted but ONE design decision is genuinely ambiguous and changes the result. Ask that
  single question; give 2-4 short answer options in "options". Never ask about things you can sensibly default.
- "plan": you understand enough to build. Fill "plan" completely, choosing sensible defaults and stating them.
  Simple, well-defined requests (e.g. an ISBN-13 validator) go straight to "plan" - do not interrogate.
  The person will see the plan as a card with "Find or build it" and "Change something" buttons.
- "find": they want to see if an agent already exists / search Agentverse. Put the capability in "query".
- "command": they asked for one of: status (their jobs), history (what FOUNDRY learned), scoreboard (measured
  improvement), evolve (run a learning round). Put it in "command".
If the person asks to change a plan, produce an updated "plan". If they say yes/go/build it right after a plan,
use intent "confirm".

Return ONLY JSON:
{{"intent": "greet|chat|clarify|plan|find|command|confirm",
 "reply": "what you say to the person (markdown ok)",
 "options": ["...", "..."],
 "query": "...",
 "command": "status|history|scoreboard|evolve",
 "plan": {{"title": "short name", "summary": "one sentence",
          "agents": [{{"name": "snake_case", "role": "what it does"}}],
          "input": "what the user sends", "output": "what comes back (text and/or which card)",
          "behaviours": ["...", "..."], "edge_cases": ["...", "..."],
          "request": "the complete build request FOUNDRY will execute: every behaviour, card, edge case and, for
                      multi-agent systems, each agent by name and who talks to whom"}}}}
Use one agent unless coordination genuinely helps; for multi-agent plans name each agent.
Games and anything random must be TESTABLE: write deterministic rules into the plan's request (e.g. for 2048: new
tile is always a 2 placed in the first empty cell scanning row by row from the top-left; "new" starts a game with two
2s in the first two cells; moves are up/down/left/right or u/d/l/r and also JSON card selections like
{{"move":"left"}}; reply with the 4x4 board as text, the score, and a detail card with four move buttons; per-user game
state in agent storage).

CONTEXT
The person's recent FOUNDRY jobs: {jobs}
"""


class ConciergeError(RuntimeError):
    pass


def respond(history: list[dict], text: str, jobs_summary: str) -> dict:
    """One conversational turn. Raises ConciergeError if no usable answer could be produced."""
    v = sk.load()
    sys_prompt = SYSTEM.format(version=v.version, jobs=jobs_summary or "none yet")
    msgs = [{"role": "system", "content": sys_prompt}] + history[-HISTORY_TURNS:] + [{"role": "user", "content": text}]
    order = [p for p in (config.env("FOUNDRY_CHAT_PROVIDER", "gpt"), "asi", "meta", "gpt") if p in config.PROVIDERS]
    seen, last = set(), None
    meter = llm.new_meter(None, name="concierge")
    for prov in order:
        if prov in seen or not llm.available(prov):
            continue
        seen.add(prov)
        try:
            r = llm.chat(msgs, provider=prov, role="concierge", meter=meter, max_tokens=2500, temperature=0.4,
                         reasoning_effort=config.env("FOUNDRY_CHAT_REASONING", "low"))
            out = parse(r["text"])
            out["_provider"] = r.get("provider")
            return out
        except Exception as e:  # noqa: BLE001 - try the next provider
            last = e
    raise ConciergeError(f"no provider produced a usable reply: {last}")


INTENTS = {"greet", "chat", "clarify", "plan", "find", "command", "confirm"}
COMMANDS = {"status", "history", "scoreboard", "evolve"}


def parse(text: str) -> dict:
    try:
        d = llm.extract_json(text)
    except Exception as e:  # noqa: BLE001
        raise ConciergeError(f"not JSON: {e}") from e
    if not isinstance(d, dict) or d.get("intent") not in INTENTS:
        raise ConciergeError(f"bad intent: {str(d)[:120]}")
    d["reply"] = str(d.get("reply") or "").strip()
    if d["intent"] == "plan":
        p = d.get("plan")
        if not isinstance(p, dict) or len(str(p.get("request", ""))) < 15:
            raise ConciergeError("plan without a usable request")
        p["agents"] = [a for a in (p.get("agents") or []) if isinstance(a, dict) and a.get("name")]
        for k in ("behaviours", "edge_cases"):
            p[k] = [str(x) for x in (p.get(k) or [])][:6]
    if d["intent"] == "command" and d.get("command") not in COMMANDS:
        d["intent"] = "chat"
    if d["intent"] == "clarify":
        d["options"] = [str(o)[:60] for o in (d.get("options") or []) if str(o).strip()][:4]
    if not d["reply"] and d["intent"] in ("greet", "chat", "clarify"):
        raise ConciergeError("empty reply")
    return d


def fallback_plan(text: str) -> dict:
    """Deterministic plan when no model is reachable: the user's words, unchanged, become the request."""
    from .. import contract
    agents = []
    try:
        c = contract.derive(text)
        agents = [{"name": n, "role": ""} for n in (c or {}).get("agents", [])] if c else []
    except Exception:  # noqa: BLE001
        agents = []
    title = re.sub(r"^(please\s+)?(build|create|make|write|develop)\s+(me\s+)?(an?\s+)?", "", text, flags=re.I)
    return {"title": title[:60].strip().capitalize() or "Your agent", "summary": text[:200], "agents": agents,
            "input": "chat message", "output": "chat reply", "behaviours": [], "edge_cases": [], "request": text}


def plan_rows(p: dict) -> list[tuple[str, str]]:
    agents = p.get("agents") or []
    if len(agents) > 1:
        a = "; ".join(f"{x['name']}: {x.get('role', '')}".rstrip(": ") for x in agents)
    else:
        a = "single agent"
    rows = [("What it does", p.get("summary") or p.get("title", "")), ("Agents", a)]
    if p.get("input"):
        rows.append(("You send", p["input"]))
    if p.get("output"):
        rows.append(("It replies with", p["output"]))
    if p.get("behaviours"):
        rows.append(("Behaviour", " · ".join(p["behaviours"])))
    if p.get("edge_cases"):
        rows.append(("Edge cases", " · ".join(p["edge_cases"])))
    rows.append(("How it's proven", "hidden tests by an independent model + adversarial cases, run over the real "
                                    "chat protocol; nothing ships unless all pass"))
    rows.append(("Before building", "the Scout re-tests existing Agentverse agents live and offers reuse if one "
                                    "really works"))
    return [(k, str(v)[:400]) for k, v in rows]


def jobs_summary(jobs: list[dict]) -> str:
    out = []
    for j in jobs[-6:]:
        res = j.get("result") or {}
        out.append(f"{j['job_id']}: \"{str(j.get('request', ''))[:80]}\" status={j.get('status')}"
                   + (f" tests={res.get('passed')}/{res.get('cases')}" if res.get("cases") else ""))
    return "; ".join(out)
