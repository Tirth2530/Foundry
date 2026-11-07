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
    """Judge one live reply ({texts, acked, latency_s, ...}) against the case's first turn."""
    turn = case["turns"][0]
    rec = {"replies": [{"texts": reply.get("texts", []), "metadata": [], "end_session": False}]
           if reply.get("texts") else [], "acked": reply.get("acked")}
    checks = [c for c in turn.get("checks", []) if c["type"] in LIVE_CHECK_TYPES]
    res = dsl.evaluate_turn({"send": turn["send"], "checks": checks}, rec)
    content = [c for c in res if c["type"] != "ack"]
    # an acknowledgement alone proves liveness, not behaviour: require at least one content check
    passed = bool(content) and all(c["ok"] for c in res)
    return {**reply, "checks": res, "passed": passed, "case": case.get("name")}


def run(client: agentverse.Client, address: str, case: dict | None) -> dict:
    if not case:
        return {"skipped": True, "passed": None}
    reply = client.invoke(address, case["turns"][0]["send"])
    return judge(case, reply)
