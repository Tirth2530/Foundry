"""Collaboration checks for multi-agent systems, from traffic the sandbox actually observed.

A single agent can fake a multi-agent spec's outputs. These checks fail it unless the named agents
exist and the required ones really received messages from another generated agent."""
from __future__ import annotations

from .dsl import TOPOLOGY


def observed_edges(raw: dict) -> set:
    return {(t["from"], t["to"]) for t in raw.get("traffic") or []}


def check_spec(topology: dict | None, raw: dict, built: list[str]) -> list[dict]:
    """Benchmark requirement: {"agents": [names that must exist], "must_receive": [names]}."""
    if not topology:
        return []
    fails = []
    missing = [n for n in topology.get("agents", []) if n not in built]
    if missing:
        fails.append({"case": "topology", "send": "", "code": TOPOLOGY,
                      "detail": f"required agents {missing} were not built (built: {built}); name agents exactly as the request does"})
    if not raw.get("traffic_tap"):
        return fails  # traffic not observable on this uagents version: reported as unverified, see verify_note
    edges = observed_edges(raw)
    for n in topology.get("must_receive", []):
        if n in built and not any(dst == n and src not in ("client", n) for src, dst in edges):
            fails.append({"case": "topology", "send": "", "code": TOPOLOGY,
                          "detail": f"agent '{n}' never received a message from another agent: the system is not "
                                    "actually collaborating (one agent did all the work)"})
    return fails


def check_generic(raw: dict, built: list[str], entry: str) -> list[str]:
    """Release-gate rule for ANY multi-agent build: every non-entry agent must be used by another agent."""
    if len(built) < 2 or not raw.get("traffic_tap"):
        return []
    edges = observed_edges(raw)
    return [f"agent '{n}' never received a message from another agent during testing (dead or bypassed agent)"
            for n in built if n != entry and not any(dst == n and src not in ("client", n) for src, dst in edges)]
