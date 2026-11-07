"""Collaboration checks for multi-agent systems, from traffic the sandbox actually OBSERVED.

FAIL CLOSED. A multi-agent build is only collaboration-PASS when, during the executed tests:
  1. every required agent exists in the built system (names come from the spec, or from the
     Architect's plan for user builds; the plan is only an input, never evidence);
  2. both traffic taps were installed (send side: ctx.send; receive side: the handler actually ran);
  3. every non-entry / must_receive agent PROCESSED (handler ran, no exception) at least one message
     whose sender is ANOTHER generated agent (not the test client, not itself);
  4. that processing happened inside the time window of a test case that PASSED, i.e. the behaviour
     the tests accepted depended on real agent-to-agent communication.
If the taps are missing the verdict is UNVERIFIED, which blocks an overall VERIFIED verdict.
A single agent that produces the outputs of three agents fails (the named agents do not exist, or
never receive anything).
"""
from __future__ import annotations

from .dsl import TOPOLOGY

PASS, FAIL, UNVERIFIED, NOT_TESTED = "PASS", "FAIL", "UNVERIFIED", "NOT TESTED"
TOPOLOGY_UNVERIFIED = "TOPOLOGY_UNVERIFIED"


def observed_edges(raw: dict) -> set:
    """Edges where a handler PROCESSED the message (receive side). Falls back to nothing, never to sends."""
    return {(r["from"], r["to"]) for r in raw.get("received_log") or [] if r.get("ok")}


def _passed_windows(raw: dict, results: list | None) -> list | None:
    wins = raw.get("case_windows")
    if not wins or results is None:
        return None
    out = []
    for w, r in zip(wins, results):
        if w and r.get("passed"):
            out.append(w)
    return out


def evaluate(raw: dict, built: list[str], entry: str, required: dict | None = None,
             results: list | None = None) -> dict:
    """Return {status, failures:[{case,send,code,detail}], evidence:{...}}.

    required = {"agents": [names that must exist], "must_receive": [names that must be used by a peer]}.
    Without `required`, every non-entry agent of a multi-agent build must be used by a peer."""
    required = required or {}
    req_agents = list(required.get("agents") or [])
    multi = len(built) > 1 or len(req_agents) > 1
    ev = {"built": list(built), "entry": entry, "required": required or None,
          "send_tap": bool(raw.get("traffic_tap")), "receive_tap": bool(raw.get("receive_tap"))}
    if not multi:
        return {"status": NOT_TESTED, "failures": [], "evidence": {**ev, "reason": "single-agent build: no "
                                                                   "collaboration requirement"}}
    fails = []

    def fail(code, detail):
        fails.append({"case": "topology", "send": "", "code": code, "detail": detail})

    missing = [n for n in req_agents if n not in built]
    if missing:
        fail(TOPOLOGY, f"required agents {missing} were not built (built: {built}); a single agent cannot stand in "
                       "for a multi-agent architecture. Name agents exactly as required")
    must = list(required.get("must_receive") or [n for n in built if n != entry])
    if not (ev["send_tap"] and ev["receive_tap"]):
        fail(TOPOLOGY_UNVERIFIED, "inter-agent traffic could not be observed (send tap "
                                  f"{ev['send_tap']}, receive tap {ev['receive_tap']}): collaboration is UNVERIFIED")
        return {"status": UNVERIFIED if not missing else FAIL, "failures": fails, "evidence": ev}

    recv = [r for r in raw.get("received_log") or [] if r.get("ok")]
    errors = [r for r in raw.get("received_log") or [] if not r.get("ok")]
    wins = _passed_windows(raw, results)
    peer_deliveries = {}
    for n in must:
        if n not in built:
            continue
        hits = [r for r in recv if r["to"] == n and r["from"] not in ("client", n) and r["from"] in built]
        peer_deliveries[n] = len(hits)
        if not hits:
            fail(TOPOLOGY, f"agent '{n}' never processed a message from another agent: the system is not actually "
                           "collaborating (one agent did the work, or the peer is dead/bypassed)")
            continue
        if wins is not None and not any(w[0] - 0.5 <= r["t"] <= w[1] + 0.5 for r in hits for w in wins):
            fail(TOPOLOGY, f"agent '{n}' only received peer messages outside every PASSING test case: the "
                           "accepted behaviour did not depend on it")
    for r in errors[:5]:
        if r["to"] in built and r["from"] in built:
            fail(TOPOLOGY, f"agent '{r['to']}' raised while processing {r['type']} from '{r['from']}': {r.get('error')}")
    ev.update(peer_deliveries=peer_deliveries, handler_errors=len(errors),
              sends_observed=len(raw.get("traffic_log") or []), processed_observed=len(recv),
              dependence_checked=wins is not None)
    return {"status": FAIL if fails else PASS, "failures": fails, "evidence": ev}


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
