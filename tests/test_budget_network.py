"""Issue 1 regression: in agent-network mode, a role agent's spend must be committed in the SAME atomic step that
releases its grant. Otherwise another job can reserve the released headroom before the spend is booked and the
project cap is exceeded.

The path under test is the real one: pipeline.rpc -> network.Transport -> RoleTask -> network.make_role_agent
handler (caller allowlist) -> network.execute (detached meter capped at the grant) -> RoleResult ->
Transport.on_result -> grant settlement. Only uAgents' wire delivery is replaced by an in-process bus (`Bus`), and
the model by the scripted fake ($0.01/call, reserve -> settle like llm.chat)."""
import asyncio
import json
import threading
import time

import pytest

from foundry import llm, pipeline, skills as sk
from foundry.agents import network


class Bus:
    """Delivers uAgents messages between agents in one event loop running on a background thread."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.agents = {}
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def ctx(self, agent):
        bus = self

        class Ctx:
            def __init__(self):
                self.agent = agent

            async def send(self, to, msg):
                target = bus.agents[to]
                handler = target.handlers[type(msg)]
                asyncio.ensure_future(handler(bus.ctx(target), agent.address, msg))
        return Ctx()

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)


@pytest.fixture
def wired(sandbox, monkeypatch):
    from uagents import Agent
    bus = Bus()
    foreman = Agent(name="foundry", seed="net-budget")
    roles = network.build_roles(foreman.address)
    for a in [foreman, *roles.values()]:
        bus.agents[a.address] = a
    t = network.install(foreman, roles)
    t.loop, t.ctx = bus.loop, bus.ctx(foreman)
    yield t
    pipeline.TRANSPORT = None
    bus.stop()


def _project(monkeypatch, cap):
    monkeypatch.setenv("FOUNDRY_PROJECT_CAP_USD", str(cap))
    llm.reset_process_budgets()
    return llm.project_meter()


def test_concurrent_builds_over_the_agent_network_never_exceed_the_project_cap(wired, monkeypatch):
    # each scripted build costs ~$0.04 and may hold a $0.06 grant; the $0.20 project cap fits ~3 grants at once
    # and ~5 builds in total, so 8 concurrent builds both overlap and hit the cap
    proj = _project(monkeypatch, 0.20)
    results, executed, peak = [], [], [0.0]
    real_execute = network.execute
    monkeypatch.setattr(network, "execute", lambda task: executed.append(task["role"]) or real_execute(task))
    real_reserve = llm.CostMeter.reserve

    def reserve(self, usd, provider=None):
        g = real_reserve(self, usd, provider)
        peak[0] = max(peak[0], proj.reserved)
        return g
    monkeypatch.setattr(llm.CostMeter, "reserve", reserve)

    def one(i):
        results.append(pipeline.build(f"an agent number {i}", sk.load(), budget_usd=0.06))
    ts = [threading.Thread(target=one, args=(i,)) for i in range(8)]
    [x.start() for x in ts]
    [x.join(60) for x in ts]
    assert len(results) == 8
    assert {"architect", "examiner", "builder", "runner"} <= set(executed)  # really went through role agents
    assert peak[0] > 0.06 + 1e-9                 # >= 2 grants were held at the same time: real concurrency
    assert proj.spent <= proj.cap_usd + 1e-9, proj.spent
    assert proj.reserved == pytest.approx(0.0)                       # every grant closed
    booked = sum(r["cost"]["total_usd"] for r in results)
    assert booked == pytest.approx(proj.spent)                         # nothing lost, nothing double counted
    assert any(r["status"] == "budget_exceeded" for r in results)      # the cap actually bit
    assert not any("unreconciled" in r["cost"]["by_role"] for r in results)  # every role agent reported usage


def test_spend_is_booked_before_released_headroom_can_be_reserved(wired, monkeypatch):
    """Force the exact interleaving from the audit: job B tries to reserve while job A's role agent has returned
    but its usage is not yet booked. A correct implementation leaves B no room (A's spend is already booked);
    the old release-then-add() code let B reserve A's spend a second time."""
    proj = _project(monkeypatch, 0.05)
    a, b = llm.new_meter(1.0, name="job A"), llm.new_meter(1.0, name="job B")
    b_result = {}

    def b_reserves():
        try:
            g = b.reserve(0.04)
            b_result["reserved"] = g["usd"]
            b.settle(g, 0.04)
        except llm.BudgetExceeded as e:
            b_result["refused"] = str(e)
    orig_add = llm.CostMeter.add

    def add_after_competitor(self, rec):         # only reached if spend is booked OUTSIDE the release step
        th = threading.Thread(target=b_reserves)
        th.start()
        th.join()
        return orig_add(self, rec)
    monkeypatch.setattr(llm.CostMeter, "add", add_after_competitor)

    def wire(role, payload):                     # role agent spent $0.045 of its grant
        return {"ok": True, "result": "x", "calls": [{"role": role, "provider": "gpt", "model": "m", "tokens_in": 1,
                                                      "tokens_out": 1, "usd": 0.045}]}
    monkeypatch.setattr(pipeline, "TRANSPORT", wire)
    pipeline.rpc("builder", "build", None, skills=sk.load(), meter=a, request="x", plan={})
    if not b_result:                             # fixed code never calls add(): run B now, after settlement
        b_reserves()
    assert "refused" in b_result, b_result
    assert proj.spent == pytest.approx(0.045) and proj.spent <= proj.cap_usd


def test_role_agent_that_never_reports_is_charged_the_whole_grant(sandbox, monkeypatch):
    proj = _project(monkeypatch, 1.0)
    m = llm.new_meter(0.3, name="job")
    monkeypatch.setattr(pipeline, "TRANSPORT", lambda role, payload: {"ok": False, "error": "builder timed out"})
    with pytest.raises(llm.LLMError):
        pipeline.rpc("builder", "build", None, skills=sk.load(), meter=m, request="x", plan={})
    assert m.total_usd == pytest.approx(0.3) and proj.spent == pytest.approx(0.3)   # unknown spend != zero
    assert m.calls[-1]["usage_source"] == "unreconciled" and m.reserved == 0


def test_transport_exception_also_settles_the_grant(sandbox, monkeypatch):
    proj = _project(monkeypatch, 1.0)
    m = llm.new_meter(0.2, name="job")

    def boom(role, payload):
        raise RuntimeError("bus down")
    monkeypatch.setattr(pipeline, "TRANSPORT", boom)
    with pytest.raises(RuntimeError):
        pipeline.rpc("builder", "build", None, skills=sk.load(), meter=m, request="x", plan={})
    assert proj.reserved == 0 and proj.spent == pytest.approx(0.2)


def test_role_agent_meter_is_capped_at_its_grant(sandbox):
    """The remote side cannot spend more than it was granted (scripted model costs $0.01 per call)."""
    out = network.execute({"role": "architect", "op": "plan", "kwargs": {"request": "x"}, "cap_remaining": 0.005,
                           "skills_version": "v0000", "skills_path": str(sk.VERSIONS / "v0000")})
    assert out["ok"] is False and out.get("budget") and out["calls"] == []


def test_known_limit_a_grant_as_large_as_project_headroom_serialises_jobs(wired, monkeypatch):
    """Documented behaviour, not a bug we hide: a grant reserves the job's whole remaining headroom. When that equals
    the project headroom, a second concurrent job is refused (fails closed) instead of overspending."""
    proj = _project(monkeypatch, 0.25)
    a = llm.new_meter(1.0, name="job A")
    held = a.reserve(a.headroom())               # job A's role agent is mid-call holding the whole project room
    res = pipeline.build("an agent", sk.load(), budget_usd=1.0)
    assert res["status"] == "budget_exceeded" and proj.spent == 0
    a.settle_grant(held, [])
