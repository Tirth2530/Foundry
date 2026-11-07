"""Scout: before anything is built, find an EXISTING agent that already does the job, and
re-prove it live before recommending it. Nothing is reused on the strength of a README or a
search ranking.

Candidate sources, in order of trust:
  1. FOUNDRY's verified catalog: agents FOUNDRY built, tested and published (from the ledger),
     each with the independent tests it originally passed.
  2. Agentverse search (optional hint only). The search API is not production-ready, so any
     error or empty result simply falls back to the catalog/build path.

Re-verification: the best match receives one test message LIVE through a relay agent.
  - catalog agents are re-run against their ORIGINAL independent test
  - external agents are tested against the NEW request (tests written by the Spec Examiner
    from the request only), i.e. "does it do what this user needs", not "what it claims"
Outcomes are recorded in the ledger either way, including external agents that fail their claim.
"""
from __future__ import annotations

import json
import re

from .. import agentverse, config, ledger, llm
from . import examiner, livecheck, recovery

MATCH_SYSTEM = """You decide whether an existing agent already satisfies a user's request. You get the request
and candidate agents (id, source, description). Pick the single best candidate ONLY if it would satisfy the request
as written (same inputs, same output format, same features such as cards or memory). Otherwise return null.
Return JSON only: {"best": "<id or null>", "confidence": 0.0-1.0, "reason": "...", "gaps": ["what it would not do"]}"""

WORD = re.compile(r"[a-z0-9]+")
STOP = set("a an the and or to of for with that it is in on as by be when if reply user agent agents "
           "build this from into each one any are its their your you should will".split())


def _tokens(text: str) -> set:
    return {w for w in WORD.findall(text.lower()) if w not in STOP and len(w) > 2}


def catalog() -> list[dict]:
    """Every agent FOUNDRY published whose live call passed, newest first, deduplicated by job."""
    seen, out = set(), []
    for r in reversed(ledger.read("publish")):
        if r["job_id"] in seen or not r.get("live_passed"):
            continue
        seen.add(r["job_id"])
        exam_p = config.RUNS / r["job_id"] / "examiner_tests.json"
        exam = json.loads(exam_p.read_text()) if exam_p.exists() else {"cases": []}
        out.append({"id": f"foundry:{r['job_id']}", "source": "foundry_catalog", "address": r.get("entry"),
                    "job_id": r["job_id"], "description": r.get("request", ""),
                    "verified": r.get("verify"), "cases": exam.get("cases", [])})
    return out


def external(request: str, client: agentverse.Client | None, limit: int = 6) -> tuple[list[dict], str]:
    if client is None or config.env("FOUNDRY_USE_SEARCH", "true") != "true":
        return [], "search disabled"
    try:
        hits = client.search(request[:200], limit=limit)
    except Exception as e:  # noqa: BLE001 - search is a hint, never a dependency
        return [], f"search unavailable ({type(e).__name__})"
    out = []
    for h in hits or []:
        addr = h.get("address") if isinstance(h, dict) else None
        if not addr or not str(addr).startswith("agent1"):
            continue
        desc = " ".join(str(h.get(k, "")) for k in ("name", "description", "short_description", "readme"))[:1200]
        out.append({"id": f"av:{addr[:20]}", "source": "agentverse_search", "address": addr,
                    "name": h.get("name"), "description": desc})
    return out, f"search returned {len(out)} candidate(s)" if out else "search returned nothing"


def shortlist(request: str, cands: list[dict], k: int = 5) -> list[dict]:
    q = _tokens(request)
    scored = []
    for c in cands:
        d = _tokens(c.get("description", ""))
        if not d:
            continue
        score = len(q & d) / max(1, len(q | d))
        if score >= 0.12:
            scored.append((score, c))
    return [c for _, c in sorted(scored, key=lambda x: -x[0])[:k]]


def find(request: str, skills, meter, events=None, client: agentverse.Client | None = None) -> dict:
    """Return {"found": bool, "candidate"?, "live"?, "match"?, "considered", "search_note"}."""
    if client is None and config.AGENTVERSE_API_KEY:
        try:
            client = agentverse.Client()
        except agentverse.AgentverseError:
            client = None
    cat = catalog()
    ext, note = external(request, client)
    short = shortlist(request, cat + ext)
    result = {"found": False, "considered": {"catalog": len(cat), "search": len(ext), "shortlist": len(short)},
              "search_note": note}
    if not short:
        return result

    listing = "\n".join(f"- id={c['id']} source={c['source']}: {c['description'][:500]}" for c in short)

    def go(p):
        r = llm.chat([{"role": "system", "content": MATCH_SYSTEM},
                      {"role": "user", "content": f"Request:\n{request}\n\nCandidates:\n{listing}"}],
                     provider=p, role="scout", meter=meter, max_tokens=500)
        return llm.extract_json(r["text"])
    match = recovery.call(go, provider=skills.route("scout"), events=events)
    best = next((c for c in short if c["id"] == match.get("best")), None)
    result["match"] = {k: match.get(k) for k in ("confidence", "reason", "gaps")}
    if not best or float(match.get("confidence") or 0) < 0.7:
        return result
    result["candidate"] = {k: v for k, v in best.items() if k != "cases"}
    if client is None:
        result["live"] = {"skipped": True, "passed": None, "why": "no Agentverse key for live re-check"}
        return result

    if best["source"] == "foundry_catalog":
        case = livecheck.pick_case(best["cases"])
        basis = "its original independent test"
    else:
        exam = examiner.write_tests(request, skills, meter, events)
        case = livecheck.pick_case(exam["cases"])
        basis = f"a test written by {exam.get('provider')} from your request"
    live = livecheck.run(client, best["address"], case)
    live["basis"] = basis
    result["live"] = live
    result["found"] = bool(live.get("passed"))
    ledger.append("scout_check", address=best["address"], source=best["source"], passed=live.get("passed"),
                  confidence=match.get("confidence"), basis=basis, request=request[:300])
    return result
