"""Collaboration checks for multi-agent systems, from traffic the sandbox actually OBSERVED.

FAIL CLOSED. A multi-agent build is only collaboration-PASS when, during the executed tests:
  1. every required agent exists in the built system, and at least the required NUMBER of agents was built.
     Requirements come from the benchmark spec or the user's approved contract (foundry/contract.py); the
     Architect's plan is used only when neither exists, and is never evidence;
  2. both traffic taps were installed (send side: ctx.send; receive side: the handler actually ran);
  3. every required peer has at least one MATCHED delivery: a send record and a processed-receive record
     (handler ran, no exception) that agree on sender, receiver and correlation id, where the sender is ANOTHER
     generated agent (not the test client, not itself) and the send did not error;
  4. that matched delivery (send AND receive) lies inside the time window of a test case that PASSED.

Verdicts: FAIL when the evidence shows the requirement is not met (agent missing, too few agents, a peer that
never processed anything, a handler that crashed, deliveries only outside passing tests). UNVERIFIED when the
evidence is incomplete (a tap missing, a receive with no matching send, no test windows / results to tie it to a
passing test). FAIL outranks UNVERIFIED outranks PASS. Only PASS (or NOT TESTED for a single agent with no
multi-agent requirement) lets a build be VERIFIED.
"""
from __future__ import annotations

from .dsl import TOPOLOGY

PASS, FAIL, UNVERIFIED, NOT_TESTED = "PASS", "FAIL", "UNVERIFIED", "NOT TESTED"
TOPOLOGY_UNVERIFIED = "TOPOLOGY_UNVERIFIED"
SLACK_S = 0.5  # clock slack around a case window (send/receive bookkeeping happens just before/after the turn)


def observed_edges(raw: dict) -> set:
    """Edges with a matched send + processed receive (for display). Never sends alone, never receives alone."""
    sends = {(t["from"], t["to"], t.get("corr")) for t in raw.get("traffic_log") or [] if not t.get("send_error")}
    return {(r["from"], r["to"]) for r in raw.get("received_log") or []
            if r.get("ok") and (r["from"], r["to"], r.get("corr")) in sends}


def _passed_windows(raw: dict, results: list | None) -> list | None:
    wins = raw.get("case_windows")
    if not wins or results is None or len(wins) != len(results):
        return None
    return [w for w, r in zip(wins, results) if w and r.get("passed")]


def _in(t, wins) -> bool:
    return t is not None and any(w[0] - SLACK_S <= t <= w[1] + SLACK_S for w in wins)


def evaluate(raw: dict, built: list[str], entry: str, required: dict | None = None,
             results: list | None = None) -> dict:
    """Return {status, failures:[{case,send,code,detail}], evidence:{...}}.

    required = {"agents": [names that must exist], "min_agents": N, "must_receive": [names a peer must use],
                "source": where the requirement came from}.
    Without must_receive, every non-entry agent of a multi-agent build must be used by a peer."""
    required = required or {}
    req_agents = list(required.get("agents") or [])
    min_agents = int(required.get("min_agents") or len(req_agents) or 0)
    multi = len(built) > 1 or len(req_agents) > 1 or min_agents > 1
    ev = {"built": list(built), "entry": entry, "required": required or None,
          "send_tap": bool(raw.get("traffic_tap")), "receive_tap": bool(raw.get("receive_tap"))}
    if not multi:
        return {"status": NOT_TESTED, "failures": [], "evidence": {**ev, "reason": "single-agent build and no "
                                                                   "multi-agent requirement"}}
    fails, unverified = [], []

    def fail(detail):
        fails.append({"case": "topology", "send": "", "code": TOPOLOGY, "detail": detail})

    def unv(detail):
        unverified.append({"case": "topology", "send": "", "code": TOPOLOGY_UNVERIFIED, "detail": detail})

    def verdict():
        return {"status": FAIL if fails else (UNVERIFIED if unverified else PASS), "failures": fails + unverified,
                "evidence": ev}

    missing = [n for n in req_agents if n not in built]
    if missing:
        fail(f"required agents {missing} were not built (built: {built}); a single agent cannot stand in for a "
             "multi-agent architecture. Name agents exactly as required")
    if len(built) < min_agents:
        fail(f"{required.get('source') or 'the requirement'} asks for at least {min_agents} agents but only "
             f"{len(built)} were built ({built})")
    if not (ev["send_tap"] and ev["receive_tap"]):
        unv(f"inter-agent traffic could not be observed (send tap {ev['send_tap']}, receive tap {ev['receive_tap']}): "
            "collaboration is UNVERIFIED")
        return verdict()

    sends = [t for t in raw.get("traffic_log") or []]
    recv = [r for r in raw.get("received_log") or [] if r.get("ok")]
    errors = [r for r in raw.get("received_log") or [] if not r.get("ok")]
    wins = _passed_windows(raw, results)
    must = list(required.get("must_receive") or [n for n in built if n != entry])
    per_peer = {}
    for n in must:
        if n not in built:
            continue
        hits = [r for r in recv if r["to"] == n and r["from"] in built and r["from"] not in ("client", n)]
        matched = []
        for r in hits:
            s = next((x for x in sends if x["from"] == r["from"] and x["to"] == n and x.get("corr")
                      and x.get("corr") == r.get("corr") and not x.get("send_error")), None)
            if s is not None:
                matched.append((s, r))
        in_pass = [] if wins is None else [(s, r) for s, r in matched if _in(s.get("t"), wins) and _in(r.get("t"), wins)]
        per_peer[n] = {"processed": len(hits), "matched": len(matched), "in_passing_test": len(in_pass)}
        if not hits:
            fail(f"agent '{n}' never processed a message from another agent: the system is not actually collaborating "
                 "(one agent did the work, or the peer is dead/bypassed)")
        elif not matched:
            unv(f"agent '{n}' processed {len(hits)} peer message(s) but none has a matching send record (same sender, "
                "receiver and correlation id): the delivery cannot be confirmed")
        elif wins is None:
            unv(f"agent '{n}' has matched deliveries but there are no test windows/results to tie them to a passing "
                "test")
        elif not in_pass:
            fail(f"agent '{n}' only exchanged messages outside every PASSING test case: the accepted behaviour did not "
                 "depend on it")
    for r in errors[:5]:
        if r["to"] in built and r["from"] in built:
            fail(f"agent '{r['to']}' raised while processing {r['type']} from '{r['from']}': {r.get('error')}")
    ev.update(peers=per_peer, handler_errors=len(errors), sends_observed=len(sends), processed_observed=len(recv),
              dependence_checked=wins is not None)
    return verdict()


def check_spec(topology: dict | None, raw: dict, built: list[str], entry: str | None = None,
               results: list | None = None) -> list[dict]:
    """Benchmark requirement -> failure list (empty == collaboration PASS). Fail-closed when unobservable."""
    if not topology:
        return []
    out = evaluate(raw, built, entry or (built[0] if built else ""), topology, results)
    return out["failures"]


def check_generic(raw: dict, built: list[str], entry: str, results: list | None = None) -> list[str]:
    """Release-gate rule for ANY multi-agent build: every non-entry agent must be used by another agent."""
    out = evaluate(raw, built, entry, None, results)
    return [f["detail"] for f in out["failures"]]


def required_from_plan(plan: dict | None, built: list[str]) -> dict | None:
    """The Architect's plan names the agents the system claims to have. That claim is not evidence, but it IS
    a requirement: if the design says three agents, three must exist and the peers must really be used."""
    names = [str(a.get("name")) for a in (plan or {}).get("agents", []) if isinstance(a, dict) and a.get("name")]
    if len(names) < 2:
        return None
    entry = (plan or {}).get("entry")
    return {"agents": names, "must_receive": [n for n in names if n != entry]}
