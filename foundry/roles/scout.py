"""Scout: searches Agentverse (and FOUNDRY's own verified catalog) for an agent that already does
the job, then RE-RANKS the results by live evidence. Search finds candidates; only executed
tests decide what is trusted.

Pipeline
  1. catalog      agents FOUNDRY itself built, verified and published (from the ledger)
  2. search       Agentverse marketplace search, queried several ways (the model writes 3 short
                  queries; semantic + lexical), with retries; filtered to active, responsive,
                  chat-capable agents; deduplicated. If search is down, steps 1 and 5 still work.
  3. shortlist    lexical overlap with the request, then the model ranks the top 3 and names gaps
  4. probes       per candidate, in ONE relay agent on Agentverse:
                    NEED : catalog -> its original independent test; external -> a test the Spec
                           Examiner wrote from the user's request ("does it do what you need?")
                    CLAIM: external with a README -> one probe derived from its own README
                           ("does it do what it says?")
  5. verdicts     every probe judged by the same test language as the sandbox; the platform's
                  own metrics (rating, success rate) are shown next to what we observed
Every audit is written to the ledger, including agents that fail their claims.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor

from .. import agentverse, config, dsl, ledger, llm
from . import examiner, livecheck, recovery

MATCH_SYSTEM = """You decide which existing agents could satisfy a user's request. You get the request and candidate
agents (id, source, description). Rank at most 3 candidates that would plausibly satisfy the request as written
(same inputs, outputs and features). Return JSON only:
{"ranked": [{"id": "...", "confidence": 0.0-1.0, "reason": "...", "gaps": ["..."]}]}   (empty list if none fit)"""

QUERY_SYSTEM = """Write 3 short, different search queries (2-5 words each) to find an existing AI agent on a
marketplace that does what the user asks. Return JSON only: {"queries": ["...", "...", "..."]}"""

CLAIM_SYSTEM = """From an agent's README, pick ONE concrete, testable claim and write a single chat message that
exercises it, plus text checks a correct reply must satisfy. Only assert what the README states. Checks may be:
{"type":"text_contains","value":"..."} | {"type":"text_regex","pattern":"..."} | {"type":"text_number","value":1.0,"tol":0.01}
Return JSON only: {"claim": "...", "send": "...", "checks": [...]}  or {"claim": null} if nothing is testable."""

WORD = re.compile(r"[a-z0-9]+")
STOP = set("a an the and or to of for with that it is in on as by be when if reply user agent agents "
           "build this from into each one any are its their your you should will".split())
MAX_CANDIDATES = 3


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
                    "name": f"FOUNDRY {r['job_id']}", "job_id": r["job_id"], "description": r.get("request", ""),
                    "cases": exam.get("cases", [])})
    return out


def _llm_json(system: str, user: str, skills, meter, events, max_tokens=2000):
    def go(p):
        r = llm.chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                     provider=p, role="scout", meter=meter, max_tokens=max_tokens)
        return llm.extract_json(r["text"])
    return recovery.call(go, provider=skills.route("scout"), attempts=2, events=events)


def search_agentverse(request: str, client, skills, meter, events) -> tuple[list[dict], dict]:
    report = {"queries": [], "raw_hits": 0, "kept": 0, "errors": []}
    if client is None or config.env("FOUNDRY_USE_SEARCH", "true") != "true":
        report["errors"].append("search disabled (no Agentverse key)")
        return [], report
    try:
        qs = _llm_json(QUERY_SYSTEM, request, skills, meter, events).get("queries", [])[:3]
    except llm.BudgetExceeded:
        raise
    except Exception:  # noqa: BLE001
        qs = []
    queries = [q for q in [*qs, " ".join(list(_tokens(request))[:6])] if isinstance(q, str) and q.strip()]
    report["queries"] = queries
    found: dict = {}

    def one(args):
        q, semantic = args
        try:
            return client.search_agents(q, limit=15, semantic=semantic)
        except agentverse.AgentverseError as e:
            report["errors"].append(f"{q!r}: {str(e)[:120]}")
            return []
    with ThreadPoolExecutor(max_workers=4) as ex:
        for hits in ex.map(one, [(q, s) for q in queries for s in (True, False)]):
            for h in hits or []:
                report["raw_hits"] += 1
                addr = h.get("address") if isinstance(h, dict) else None
                name = str(h.get("name") or "")
                if not addr or not str(addr).startswith("agent1") or name.startswith("foundry-relay"):
                    continue
                if h.get("unresponsive") or h.get("status") not in (None, "active"):
                    continue
                if not agentverse.has_chat_protocol(h):
                    continue
                found.setdefault(addr, {
                    "id": f"av:{addr[:24]}", "source": "agentverse_search", "address": addr, "name": name or addr[:16],
                    "handle": h.get("handle"), "readme": str(h.get("readme") or "")[:3000],
                    "description": " ".join(str(h.get(k) or "") for k in ("name", "description", "readme"))[:1500],
                    "platform": {k: h.get(k) for k in ("rating", "recent_success_rate", "recent_eval_success_rate",
                                                       "total_interactions") if h.get(k) is not None}})
    report["kept"] = len(found)
    return list(found.values()), report


def shortlist(request: str, cands: list[dict], k: int = 8) -> list[dict]:
    q = _tokens(request)
    scored = []
    for c in cands:
        d = _tokens(c.get("description", ""))
        if d:
            score = len(q & d) / max(1, len(q | d))
            if score >= 0.08:
                scored.append((score, c))
    return [c for _, c in sorted(scored, key=lambda x: -x[0])[:k]]


def _claim_probe(c: dict, skills, meter, events) -> dict | None:
    if c["source"] != "agentverse_search" or len(c.get("readme", "")) < 40:
        return None
    try:
        d = _llm_json(CLAIM_SYSTEM, c["readme"], skills, meter, events, max_tokens=2000)
    except llm.BudgetExceeded:
        raise
    except Exception:  # noqa: BLE001
        return None
    if not d.get("claim") or not d.get("send"):
        return None
    case = {"name": "claim", "claim": d["claim"], "turns": [{"send": str(d["send"])[:800],
                                                              "checks": [{"type": "ack"}, *d.get("checks", [])]}]}
    return case if not dsl.validate_case(case) else None


def find(request: str, skills, meter, events=None, client=None) -> dict:
    """Returns {"found", "candidate"?, "live"?, "match"?, "audits": [...], "considered", "search", "search_note"}."""
    events = events if events is not None else []
    if client is None and config.AGENTVERSE_API_KEY:
        try:
            client = agentverse.Client()
        except agentverse.AgentverseError:
            client = None
    cat = catalog()
    ext, srep = search_agentverse(request, client, skills, meter, events)
    short = shortlist(request, cat + ext)
    note = (f"{len(srep['queries'])} queries, {srep['raw_hits']} hits, {srep['kept']} live chat agents kept"
            + (f", {len(srep['errors'])} search errors" if srep["errors"] else ""))
    result = {"found": False, "audits": [], "search": srep, "search_note": note,
              "considered": {"catalog": len(cat), "search": len(ext), "shortlist": len(short)}}
    if not short:
        return result

    listing = "\n".join(f"- id={c['id']} source={c['source']}: {c['description'][:450]}" for c in short)
    ranked = _llm_json(MATCH_SYSTEM, f"Request:\n{request}\n\nCandidates:\n{listing}", skills, meter, events,
                       max_tokens=3000).get("ranked", [])
    by_id = {c["id"]: c for c in short}
    chosen = [(by_id[r["id"]], r) for r in ranked if r.get("id") in by_id
              and float(r.get("confidence") or 0) >= 0.6][:MAX_CANDIDATES]
    result["match"] = {k: (ranked[0] if ranked else {}).get(k) for k in ("confidence", "reason", "gaps")}
    if not chosen:
        return result
    if client is None:
        result["candidate"] = {k: v for k, v in chosen[0][0].items() if k not in ("cases", "readme")}
        result["live"] = {"skipped": True, "passed": None}
        return result

    need_case_ext = None
    if any(c["source"] == "agentverse_search" for c, _ in chosen):
        need_case_ext = livecheck.pick_case(examiner.write_tests(request, skills, meter, events)["cases"])
    probes, cases = [], {}
    for n, (c, r) in enumerate(chosen):
        need = livecheck.pick_case(c["cases"]) if c["source"] == "foundry_catalog" else need_case_ext
        if need:
            pid = f"{n}:need"
            probes.append({"id": pid, "target": c["address"], "send": need["turns"][0]["send"]})
            cases[pid] = need
        claim = _claim_probe(c, skills, meter, events)
        if claim:
            pid = f"{n}:claim"
            probes.append({"id": pid, "target": c["address"], "send": claim["turns"][0]["send"]})
            cases[pid] = claim
    replies = client.invoke_many(probes) if probes else {}

    for n, (c, r) in enumerate(chosen):
        need = livecheck.judge(cases[f"{n}:need"], replies[f"{n}:need"]) if f"{n}:need" in replies else None
        claim = livecheck.judge(cases[f"{n}:claim"], replies[f"{n}:claim"]) if f"{n}:claim" in replies else None
        audit = {"candidate": {k: v for k, v in c.items() if k not in ("cases", "readme")},
                 "match": r, "need": need, "claim": claim,
                 "need_basis": "its original independent test" if c["source"] == "foundry_catalog"
                 else "a test written from your request by the independent Spec Examiner",
                 "claim_text": cases.get(f"{n}:claim", {}).get("claim")}
        result["audits"].append(audit)
        ledger.append("scout_audit", address=c["address"], source=c["source"], name=c.get("name"),
                      need_passed=(need or {}).get("passed"), claim_passed=(claim or {}).get("passed"),
                      claim=audit["claim_text"], platform=c.get("platform"), request=request[:300])
        if not result["found"] and need and need.get("passed"):
            result.update(found=True, candidate=audit["candidate"], live={**need, "basis": audit["need_basis"]},
                          match={k: r.get(k) for k in ("confidence", "reason", "gaps")})
    if not result["found"] and result["audits"]:
        a = result["audits"][0]
        result["candidate"] = a["candidate"]
        result["live"] = {**(a["need"] or {"passed": None, "skipped": True}), "basis": a["need_basis"]}
    return result
