"""User topology contract: what the USER asked for, captured before the build and enforced by the release gate.

Why: the Architect is a model. If it proposes one agent for a request that demands three, a requirement derived
from its plan disappears and the collaboration check would report NOT TESTED. The required topology therefore comes
from the user's own words, independently of every model that designs, builds or tests the system:

  derive()    deterministic parse of the request (+ optional explicit agent names from the request form):
              a stated count ("Build THREE agents", "3 agents", "multi-agent", "an X Agent, a Y Agent ...") and,
              only when they are unambiguous, the agent names ('catalog' knows ..., a 'parser' agent ...).
              No model is involved, so neither the Architect nor the Builder can change it.
  validate()  structural rules (identifiers, uniqueness, count consistent with names, entry among names).
  seal()      canonical SHA-256. The Foreman shows the contract on the capability card; the user's approval tap
              approves THIS hash, and the pipeline refuses a contract whose hash no longer matches.

Precedence when building: benchmark spec topology > user contract > Architect plan (fallback only).
"""
from __future__ import annotations

import hashlib
import json
import re

NUMBER_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8}
COUNT_RE = re.compile(r"\b(two|three|four|five|six|seven|eight|[2-8])\s+(?:[\w-]+\s+){0,3}?(?:u?agents)\b", re.I)
MULTI_RE = re.compile(r"\bmulti[- ]agent\b|\bmultiple agents\b|\bseveral agents\b|\b(?:network|team|system) of agents\b",
                      re.I)
TITLED_RE = re.compile(r"\b(?:[Aa]n?|[Tt]he)\s+((?:[A-Z][\w/-]*\s+){1,3})Agent\b")
IDENT = r"([a-z][a-z0-9_]{0,30})"
NAME_RES = (re.compile(r"(?:^|[.!?;:]\s+)'" + IDENT + r"'\s"),                               # 'catalog' knows ...
            re.compile(r"\b(?:an?|the)\s+'" + IDENT + r"'\s+(?:entry\s+)?agent\b", re.I))      # a 'parser' agent
ENTRY_RE = re.compile(r"'" + IDENT + r"'\s+(?:is\s+the\s+entry\b|entry\s+agent\b|agent\s+\(the\s+entry\b)", re.I)


class ContractError(ValueError):
    pass


def _names(request: str) -> list[str]:
    hits = []
    for rx in NAME_RES:
        hits += [(m.start(1), m.group(1)) for m in rx.finditer(request)]
    out = []
    for _, n in sorted(hits):
        if n not in out:
            out.append(n)
    return out


def derive(request: str, explicit_agents: list[str] | None = None, source: str = "request text") -> dict | None:
    """Return the user's multi-agent contract, or None when the request asks for no multi-agent system."""
    notes = []
    counts = [int(m.group(1)) if m.group(1).isdigit() else NUMBER_WORDS[m.group(1).lower()]
              for m in COUNT_RE.finditer(request)]
    titled = {re.sub(r"\s+", " ", m.group(1)).strip().lower() for m in TITLED_RE.finditer(request)}
    n = max(counts) if counts else 0
    if len(titled) >= 2:
        n = max(n, len(titled))
    if not n and MULTI_RE.search(request):
        n = 2
        notes.append("request says multi-agent without a number: at least 2 agents")
    names = [a.strip() for a in (explicit_agents or []) if a and a.strip()]
    if names:
        source = "agent names given on the request form"
        n = max(n, len(names))
    elif counts:
        found = _names(request)
        if len(found) == n:
            names = found
        elif found:
            notes.append(f"names {found} do not match the stated count {n}: enforcing the count only")
    if n < 2:
        return None
    entry = None
    m = ENTRY_RE.search(request)
    if m and m.group(1) in names:
        entry = m.group(1)
    c = {"schema": "foundry.contract/1", "min_agents": n, "agents": names, "entry": entry,
         "must_receive": [x for x in names if x != entry] if (names and entry) else None,
         "source": source, "notes": notes}
    validate(c)
    return seal(c)


def validate(c: dict) -> None:
    if c.get("schema") != "foundry.contract/1":
        raise ContractError("unknown contract schema")
    n, names = c.get("min_agents"), c.get("agents") or []
    if not isinstance(n, int) or n < 2:
        raise ContractError("a multi-agent contract needs min_agents >= 2")
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,30}", x) for x in names):
        raise ContractError(f"agent names must be identifiers: {names}")
    if len(set(names)) != len(names):
        raise ContractError(f"duplicate agent names: {names}")
    if names and len(names) < n:
        raise ContractError(f"{len(names)} names given for a {n}-agent contract")
    if c.get("entry") is not None and c["entry"] not in names:
        raise ContractError("entry agent must be one of the named agents")
    if c.get("must_receive") and not set(c["must_receive"]) <= set(names):
        raise ContractError("must_receive must be a subset of the named agents")


def _body(c: dict) -> dict:
    return {k: c.get(k) for k in ("schema", "min_agents", "agents", "entry", "must_receive", "source")}


def digest(c: dict) -> str:
    return hashlib.sha256(json.dumps(_body(c), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def seal(c: dict) -> dict:
    return {**c, "sha256": digest(c)}


def check_sealed(c: dict) -> None:
    """The contract the pipeline enforces must be byte-for-byte the one that was shown and approved."""
    validate(c)
    if c.get("sha256") != digest(c):
        raise ContractError("contract was altered after it was sealed/approved (sha256 mismatch)")


def requirement(c: dict | None) -> dict | None:
    """Topology requirement in the form topology.evaluate() takes."""
    if not c:
        return None
    return {"agents": list(c.get("agents") or []), "min_agents": c["min_agents"],
            "must_receive": c.get("must_receive"), "source": f"your contract ({c.get('source')})"}


def describe(c: dict | None) -> str:
    if not c:
        return "single agent: your request does not ask for a multi-agent system"
    names = f": {', '.join(c['agents'])}" if c.get("agents") else " (names not specified)"
    return (f"at least {c['min_agents']} agents{names}; every peer must really receive and process messages "
            f"during passing tests · contract {c['sha256'][:12]}")
