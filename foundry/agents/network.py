"""FOUNDRY's agent network: 15 role agents + the Foreman. Locally in a Bureau, or (FOUNDRY_NETWORK_MODE=agentverse)
as separate Agentverse mailbox agents so every task and result travels through Agentverse.

Each role is its own uAgent with its own address. The Foreman's jobs talk to them only
through RoleTask/RoleResult messages (see pipeline.TRANSPORT). Trust boundaries:
  - builder never receives tests; examiner never receives code
  - runner is the only agent that executes generated code
  - judge holds the sealed promotion set; only aggregates leave it
  - quartermaster prices and caps; publisher is the only agent with deploy rights
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from pathlib import Path

from uagents import Agent, Context

from .. import config, ledger, llm, pipeline, skills as sk
from ..harness import run as harness
from ..roles import (adversary, architect, builder, card_validator, examiner, integrator, judge, miner,
                     publisher, quartermaster, scout)
from .messages import RoleResult, RoleTask


def _evolve_round(**_):
    from .. import evolve
    lines: list[str] = []
    evolve.run(1, say=lines.append)
    return {"log": lines}


def _recovery_decide(role: str, error: str, attempt: int, **_):
    if "budget" in error.lower():
        return {"action": "fail", "why": "budget cap reached; never retried"}
    if attempt >= 2:
        return {"action": "fail", "why": f"{role} failed {attempt} times"}
    return {"action": "retry", "delay_s": 2 * attempt + 1, "why": "transient failure, bounded retry"}


def _judge_eval(version: str, **_):
    return judge.public(judge.evaluate(sk.load(version), purpose="requested via agent network"))


def _miner_mine(job_ids: list, **kw):
    builds = [json.loads((config.RUNS / j / "result.json").read_text()) for j in job_ids]
    return miner.mine(builds, kw["skills"], kw["meter"], events=kw["events"])


# role -> op -> (callable, needs_llm_context)
REGISTRY = {
    "scout": {"find": (scout.find, True)},
    "architect": {"plan": (architect.plan, True)},
    "examiner": {"write_tests": (examiner.write_tests, True)},
    "adversary": {"cases": (adversary.cases, True)},
    "builder": {"build": (builder.build, True), "repair": (builder.repair, True)},
    "integrator": {"check": (integrator.check, False)},
    "runner": {"run": (lambda **kw: harness.run(**kw), False)},
    "card_validator": {"audit": (card_validator.audit, False)},
    "quartermaster": {"quote": (lambda request, version: quartermaster.quote(request, sk.load(version)), False),
                      "reconcile": (quartermaster.reconcile, False)},
    "publisher": {"publish": (publisher.publish, False)},
    "ledger": {"append": (ledger.append, False)},
    "recovery": {"decide": (_recovery_decide, False)},
    "miner": {"mine": (_miner_mine, True)},
    "judge": {"evaluate": (_judge_eval, False)},
    "evolver": {"round": (_evolve_round, False),
                "learn": (lambda job_id: __import__("foundry.evolve", fromlist=["x"]).learn_from_job(job_id, say=lambda *_: None), False)},
}

TIMEOUTS = {"scout": 900, "runner": 900, "builder": 400, "publisher": 1200, "judge": 7200, "evolver": 14400}


class Unauthorized(PermissionError):
    pass


def _safe_skills_path(path: str) -> Path:
    """Role agents only ever load skill versions from FOUNDRY's own skill store (candidates included)."""
    p = Path(path).resolve()
    roots = [sk.VERSIONS.resolve(), sk.CANDIDATES.resolve()]
    if not any(r in p.parents for r in roots):
        raise Unauthorized(f"skills_path {path!r} is outside the skill store")
    return p


def execute(task: dict) -> dict:
    """Run one role op (in a worker thread inside the role agent)."""
    t0 = time.time()
    role, op = task["role"], task["op"]
    if op not in REGISTRY.get(role, {}):
        return {"ok": False, "error": f"unknown op {role}.{op}", "calls": [], "events": []}
    fn, needs_ctx = REGISTRY[role][op]
    # detached: the requesting job already reserved this allowance in its own budget chain and commits the
    # returned calls itself, so counting them here too would double-charge the project ceiling
    meter = llm.new_meter(task.get("cap_remaining"), name=f"{role} grant", detached=True)
    events: list = []
    try:
        kw = dict(task.get("kwargs") or {})
        if needs_ctx:
            kw.update(skills=sk.load(task["skills_version"], path=_safe_skills_path(task["skills_path"])),
                      meter=meter, events=events)
        result = fn(**kw)
        return {"ok": True, "result": result, "calls": meter.calls, "events": events, "ms": int(1000 * (time.time() - t0))}
    except llm.BudgetExceeded as e:
        return {"ok": False, "budget": True, "error": str(e), "calls": meter.calls, "events": events}
    except Exception as e:  # noqa: BLE001 - reported to the Foreman / Recovery agent
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "calls": meter.calls, "events": events}


# Roles whose operations change the world or FOUNDRY itself. Every role is allowlisted; these are listed so
# denials are visible in the evidence ledger with their consequence.
SENSITIVE = {"publisher": "deploys to Agentverse", "evolver": "changes FOUNDRY's skills",
             "judge": "opens the sealed promotion set", "runner": "executes generated code",
             "ledger": "writes evidence", "miner": "spends model budget", "builder": "spends model budget"}


ROLE_PORT_BASE = int(config.env("FOUNDRY_ROLE_PORT_BASE", "8101"))

ABOUT = {
    "scout": "searches Agentverse and FOUNDRY's verified catalog for an agent that already does the job, then "
             "re-tests the best matches LIVE before anything is reused",
    "architect": "designs the agent or multi-agent system: which agents, who talks to whom, the entry agent",
    "examiner": "writes the hidden acceptance tests from the request alone; it never sees the code and runs on a "
                "different model family from the builder",
    "adversary": "writes edge-case and hostile inputs the examiner might miss",
    "builder": "writes and repairs the uAgents code (GPT-5); it never sees the hidden tests",
    "integrator": "static safety gate: hosted-agent contract, no files/env/subprocess/eval, no module-level state",
    "runner": "the only agent that executes generated code: an isolated sandbox over the real Agent Chat Protocol",
    "card_validator": "checks every ASI:One interactive card the generated agents send against the card schema",
    "quartermaster": "prices builds from measured history, sets the spend cap, reconciles actual cost",
    "publisher": "deploys verified systems to Agentverse, live-tests them there and tears down on any failure",
    "ledger": "appends every decision to FOUNDRY's hash-chained evidence ledger",
    "recovery": "decides bounded retries when a role fails; budget failures are never retried",
    "miner": "clusters the defects caught across builds into one root cause worth learning from",
    "judge": "sealed promotion judge: accepts a skill change only with more held-out tasks solved and zero "
             "regressions",
    "evolver": "proposes the skill change (a lesson or setting) that targets the mined root cause",
}


def _identity_address(seed: str) -> str:
    """The address an Agent with this seed will have, WITHOUT creating an Agent (creating one registers it with
    this process's dispatcher, which would deliver messages locally instead of through Agentverse)."""
    try:
        from uagents.crypto import Identity
    except ImportError:  # newer layouts
        from uagents_core.identity import Identity
    return Identity.from_seed(seed, 0).address


def role_seed(role: str) -> str:
    return f"{config.FOREMAN_SEED}-role-{role}"


def role_addresses() -> dict:
    return {r: _identity_address(role_seed(r)) for r in REGISTRY}


def foreman_address() -> str:
    return _identity_address(config.FOREMAN_SEED)


def role_readme(role: str, foreman: str) -> str:
    return (f"# FOUNDRY · {role}\n\n{ABOUT.get(role, '')}.\n\n"
            "Part of **FOUNDRY**, a self-improving factory for Fetch.ai agents that proves its work: independent "
            "tests, a sandboxed run over the real Agent Chat Protocol, a release gate, a Proof Passport, and "
            "publication to Agentverse.\n\n"
            f"This agent accepts `RoleTask` messages **only from the FOUNDRY Foreman** (`{foreman}`); the sender "
            "is authenticated by the uAgents envelope signature. Anyone else is refused and logged.\n\n"
            "Talk to FOUNDRY through the Foreman in ASI:One.\n")


def make_role_agent(role: str, allowed: set | None = None, *, port: int | None = None, mailbox: bool = False,
                    readme_path: str | None = None) -> Agent:
    """A role agent only accepts RoleTask messages from `allowed` addresses (the Foreman). The sender address of
    a uAgents message is authenticated by the envelope signature, so an allowlist of addresses is an identity
    check, not a name check. Anything else is refused, logged, and never executed.
    mailbox=True makes it a real Agentverse agent (own profile, messages routed through Agentverse)."""
    kw = dict(name=f"foundry-{role}", seed=role_seed(role))
    if port:
        kw["port"] = port
    if mailbox:
        kw.update(mailbox=True, publish_agent_details=True)
        if readme_path:
            kw["readme_path"] = readme_path
    try:
        a = Agent(**kw)
    except TypeError:  # older uAgents without readme_path / publish_agent_details
        kw.pop("readme_path", None)
        kw.pop("publish_agent_details", None)
        a = Agent(**kw)
    allowed = set(allowed or ())

    @a.on_message(RoleTask, replies=RoleResult)
    async def handle(ctx: Context, sender: str, msg: RoleTask):
        if sender not in allowed or msg.role != role:
            why = "caller not authorized" if sender not in allowed else f"task addressed to role {msg.role!r}"
            ledger.append("auth_denied", role=role, sender=sender, rid=msg.rid, reason=why,
                          consequence=SENSITIVE.get(role))
            await ctx.send(sender, RoleResult(rid=msg.rid, role=role, ok=False,
                                              payload=json.dumps({"error": f"unauthorized: {why}"})))
            return
        task = json.loads(msg.payload)
        task["role"] = role
        t0 = time.time()
        log = getattr(getattr(ctx, "logger", None), "info", None) or (lambda *_: None)
        log(f"[{role}] task {msg.rid[:8]} from Foreman: {task.get('op')}")
        res = await asyncio.to_thread(execute, task)
        ok = res.pop("ok")
        spent = sum(float(c.get("usd") or 0) for c in res.get("calls") or [] if isinstance(c, dict))
        log(f"[{role}] task {msg.rid[:8]} {'done' if ok else 'FAILED'} in {time.time() - t0:.1f}s"
                        + (f", model spend ${spent:.4f}" if spent else "")
                        + ("" if ok else f": {str(res.get('error'))[:200]}"))
        await ctx.send(sender, RoleResult(rid=msg.rid, role=role, ok=ok, payload=json.dumps(res, default=str)))

    return a


class Transport:
    """Synchronous call-a-role-agent bridge used by pipeline jobs running in worker threads."""

    def __init__(self, sender_agent: Agent, addresses: dict):
        self.agent = sender_agent
        self.addr = addresses
        self.loop: asyncio.AbstractEventLoop | None = None
        self.ctx: Context | None = None
        self.pending: dict = {}

        self.rejected: list = []

        @sender_agent.on_message(RoleResult)
        async def on_result(ctx: Context, sender: str, msg: RoleResult):
            # A result is accepted only for an outstanding request id (correlation), from the exact agent that
            # request was sent to, claiming the right role. Anything else is dropped and logged.
            slot = self.pending.get(msg.rid)
            if not slot or sender != slot["expect"] or msg.role != slot["role"] or slot["event"].is_set():
                why = "unknown or completed request id" if not slot else (
                    "unexpected sender" if sender != slot["expect"] else "wrong role" if msg.role != slot["role"]
                    else "duplicate result")
                self.rejected.append({"rid": msg.rid, "sender": sender, "why": why})
                ledger.append("role_result_rejected", rid=msg.rid, sender=sender, role=msg.role, reason=why)
                return
            slot["res"] = {"ok": msg.ok, **json.loads(msg.payload)}
            slot["event"].set()

        @sender_agent.on_event("startup")
        async def remember(ctx: Context):
            self.loop = asyncio.get_running_loop()
            self.ctx = ctx

    def _once(self, role: str, payload: dict) -> dict:
        rid = uuid.uuid4().hex
        slot = {"event": threading.Event(), "res": None, "expect": self.addr[role], "role": role}
        self.pending[rid] = slot
        msg = RoleTask(rid=rid, role=role, payload=json.dumps(payload, default=str))
        asyncio.run_coroutine_threadsafe(self.ctx.send(self.addr[role], msg), self.loop)
        if not slot["event"].wait(TIMEOUTS.get(role, 300)):
            self.pending.pop(rid, None)
            return {"ok": False, "error": f"{role} timed out"}
        self.pending.pop(rid, None)
        return slot["res"]

    def __call__(self, role: str, payload: dict) -> dict:
        attempt = 0
        while True:
            attempt += 1
            res = self._once(role, payload)
            if res.get("ok") or res.get("budget") or role == "recovery":
                return res
            decision = self._once("recovery", {"op": "decide", "kwargs": {
                "role": role, "error": str(res.get("error")), "attempt": attempt}})
            d = (decision.get("result") or {}) if decision.get("ok") else {"action": "fail"}
            if d.get("action") != "retry":
                res["error"] = f"{res.get('error')} (recovery: {d.get('why', 'gave up')})"
                return res
            time.sleep(d.get("delay_s", 2))


def agentverse_roles() -> list[str]:
    """Roles hosted as Agentverse mailbox agents in FOUNDRY_NETWORK_MODE=agentverse (an Agentverse account has a
    mailbox-agent limit, so the rest run locally next to the Foreman)."""
    raw = config.env("FOUNDRY_AGENTVERSE_ROLES", "scout,architect,examiner,adversary,builder,integrator,runner")
    return [r.strip() for r in raw.split(",") if r.strip() in REGISTRY]


def build_roles(foreman_address: str, only: list | None = None) -> dict:
    return {role: make_role_agent(role, {foreman_address}) for role in REGISTRY if only is None or role in only}


def install(foreman: Agent, roles: dict) -> Transport:
    """roles: {role: Agent} (local) or {role: address} (remote Agentverse mailbox agents)."""
    t = Transport(foreman, {r: (a if isinstance(a, str) else a.address) for r, a in roles.items()})
    pipeline.TRANSPORT = t
    return t
