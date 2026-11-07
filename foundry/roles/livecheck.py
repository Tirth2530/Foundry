"""Live check: send one test message to a LIVE agent (by address, through a throwaway relay
on Agentverse) and judge the reply with the same test language as the sandbox. Used by the
Publisher (after deploy) and the Scout (before reusing any existing agent)."""
from __future__ import annotations

from .. import agentverse, dsl

LIVE_CHECK_TYPES = ("ack", "text_contains", "text_not_contains", "text_regex", "text_number")


def pick_case(cases: list[dict]) -> dict | None:
    """First single-turn case whose checks can be judged from live text alone."""
    for c in cases or []:
        if len(c.get("turns", [])) == 1 and any(k["type"] in LIVE_CHECK_TYPES[1:] for k in c["turns"][0]["checks"]):
            return c
    return (cases or [None])[0]


def run(client: agentverse.Client, address: str, case: dict | None) -> dict:
    if not case:
        return {"skipped": True, "passed": None}
    turn = case["turns"][0]
    live = client.invoke(address, turn["send"])
    rec = {"replies": [{"texts": live["texts"], "metadata": [], "end_session": False}] if live["texts"] else [],
           "acked": live["acked"]}
    checks = [c for c in turn.get("checks", []) if c["type"] in LIVE_CHECK_TYPES]
    live["checks"] = dsl.evaluate_turn({"send": turn["send"], "checks": checks}, rec)
    content = [c for c in live["checks"] if c["type"] != "ack"]
    # an acknowledgement alone proves liveness, not behaviour: require at least one content check
    live["passed"] = bool(content) and all(c["ok"] for c in live["checks"])
    live["case"] = case.get("name")
    return live
