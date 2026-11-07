"""Live check: send test messages to LIVE agents (by address, through one throwaway relay on
Agentverse) and judge the replies with the same test language as the sandbox. Used by the
Publisher (after deploy) and the Scout (before trusting any existing agent)."""
from __future__ import annotations

from .. import agentverse, dsl

LIVE_CHECK_TYPES = ("ack", "text_contains", "text_not_contains", "text_regex", "text_number")


def pick_case(cases: list[dict]) -> dict | None:
    """First single-turn case whose checks can be judged from live text alone."""
    for c in cases or []:
        if len(c.get("turns", [])) == 1 and any(k["type"] in LIVE_CHECK_TYPES[1:] for k in c["turns"][0]["checks"]):
            return c
    return None


def judge(case: dict, reply: dict) -> dict:
    """Judge one live reply ({texts, acked, latency_s, ...}) against the case's first turn.
    The relay may resend a probe while waiting, so the agent can legitimately answer the same probe more than
    once: identical replies are de-duplicated, and each distinct reply is also judged on its own (a pass needs
    ONE reply that satisfies every check, never a mix of replies)."""
    turn = case["turns"][0]
    checks = [c for c in turn.get("checks", []) if c["type"] in LIVE_CHECK_TYPES]
    texts = list(reply.get("texts") or [])
    distinct = list(dict.fromkeys(texts))
    variants = [distinct] + ([[t] for t in distinct] if len(distinct) > 1 else [])

    def one(ts):
        rec = {"replies": [{"texts": ts, "metadata": [], "end_session": False}] if ts else [],
               "acked": reply.get("acked")}
        res = dsl.evaluate_turn({"send": turn["send"], "checks": checks}, rec)
        content = [c for c in res if c["type"] != "ack"]
        # an acknowledgement alone proves liveness, not behaviour: require at least one content check
        return res, bool(content) and all(c["ok"] for c in res)

    res, passed = one(variants[0])
    for v in variants[1:]:
        if passed:
            break
        r2, p2 = one(v)
        if p2:
            res, passed = r2, True
    return {**reply, "checks": res, "passed": passed, "case": case.get("name"),
            "duplicate_replies": len(texts) - len(distinct)}


def run(client: agentverse.Client, address: str, case: dict | None) -> dict:
    if not case:
        return {"skipped": True, "passed": None}
    reply = client.invoke(address, case["turns"][0]["send"])
    return judge(case, reply)
