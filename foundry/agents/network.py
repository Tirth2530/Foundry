"""FOUNDRY's agent network: 14 role agents + the Foreman, running in one uAgents Bureau.

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
    "evolver": {"round": (_evolve_round, False)},
}

TIMEOUTS = {"scout": 400, "runner": 900, "builder": 400, "publisher": 400, "judge": 7200, "evolver": 14400}


def execute(task: dict) -> dict:
    """Run one role op (in a worker thread inside the role agent)."""
    t0 = time.time()
    role, op = task["role"], task["op"]
    fn, needs_ctx = REGISTRY[role][op]
    meter = llm.CostMeter(cap_usd=task.get("cap_remaining"))
    events: list = []
    try:
        kw = dict(task.get("kwargs") or {})
        if needs_ctx:
            kw.update(skills=sk.load(task["skills_version"], path=Path(task["skills_path"])),
                      meter=meter, events=events)
        result = fn(**kw)
        return {"ok": True, "result": result, "calls": meter.calls, "events": events, "ms": int(1000 * (time.time() - t0))}
    except llm.BudgetExceeded as e:
        return {"ok": False, "budget": True, "error": str(e), "calls": meter.calls, "events": events}
    except Exception as e:  # noqa: BLE001 - reported to the Foreman / Recovery agent
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "calls": meter.calls, "events": events}


def make_role_agent(role: str) -> Agent:
    a = Agent(name=f"foundry-{role}", seed=f"{config.FOREMAN_SEED}-role-{role}")

    @a.on_message(RoleTask, replies=RoleResult)
    async def handle(ctx: Context, sender: str, msg: RoleTask):
        task = json.loads(msg.payload)
        task["role"] = role
        res = await asyncio.to_thread(execute, task)
        ok = res.pop("ok")
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

        @sender_agent.on_message(RoleResult)
        async def on_result(ctx: Context, sender: str, msg: RoleResult):
            slot = self.pending.get(msg.rid)
            if slot:
                slot["res"] = {"ok": msg.ok, **json.loads(msg.payload)}
                slot["event"].set()

        @sender_agent.on_event("startup")
        async def remember(ctx: Context):
            self.loop = asyncio.get_running_loop()
            self.ctx = ctx

    def _once(self, role: str, payload: dict) -> dict:
        rid = uuid.uuid4().hex
        slot = {"event": threading.Event(), "res": None}
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


def build_roles() -> dict:
    return {role: make_role_agent(role) for role in REGISTRY}


def install(foreman: Agent, roles: dict) -> Transport:
    t = Transport(foreman, {r: a.address for r, a in roles.items()})
    pipeline.TRANSPORT = t
    return t
