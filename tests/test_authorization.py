"""P0.4 adversarial authorization tests: role agents, role replies, payments and global actions.

Uses the real handlers (network.make_role_agent, network.Transport, Foreman payment flow) with the uAgents
stub for message plumbing. A uAgents sender address is authenticated by its envelope signature; these tests
prove FOUNDRY then acts only for the authorized identity."""
import asyncio
import json
import threading
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from foundry import config, ledger
from foundry.agents import network
from foundry.agents.messages import RoleResult, RoleTask
from tests.test_foreman import Ctx, _texts

FOREMAN = "agent1qforeman"


def _role(role, monkeypatch):
    executed = []
    monkeypatch.setattr(network, "execute", lambda task: executed.append(task) or {"ok": True, "result": "done"})
    a = network.make_role_agent(role, {FOREMAN})
    return a, a.handlers[RoleTask], executed


def _task(role="publisher", op="publish"):
    return RoleTask(rid=uuid4().hex, role=role, payload=json.dumps({"op": op, "kwargs": {}}))


@pytest.mark.parametrize("role", ["publisher", "evolver", "judge", "runner", "ledger"])
def test_sensitive_role_agents_refuse_unauthorized_callers(sandbox, monkeypatch, role):
    a, handle, executed = _role(role, monkeypatch)
    ctx = Ctx(a)
    asyncio.run(handle(ctx, "agent1qmallory", _task(role)))
    assert executed == []                                        # never executed
    to, reply = ctx.sent[-1]
    assert to == "agent1qmallory" and reply.ok is False and "unauthorized" in reply.payload
    denied = ledger.read("auth_denied")[-1]
    assert denied["role"] == role and denied["sender"] == "agent1qmallory"


def test_authorized_caller_is_served_and_role_confusion_is_refused(sandbox, monkeypatch):
    a, handle, executed = _role("publisher", monkeypatch)
    ctx = Ctx(a)
    asyncio.run(handle(ctx, FOREMAN, _task("publisher")))
    assert len(executed) == 1 and ctx.sent[-1][1].ok is True
    asyncio.run(handle(ctx, FOREMAN, _task("evolver", "round")))  # task addressed to another role
    assert len(executed) == 1 and ctx.sent[-1][1].ok is False


def test_role_agent_refuses_skill_paths_outside_the_skill_store(sandbox, tmp_path):
    out = network.execute({"role": "architect", "op": "plan", "kwargs": {"request": "x"},
                           "skills_version": "v0000", "skills_path": str(tmp_path)})
    assert out["ok"] is False and "outside the skill store" in out["error"]
    assert network.execute({"role": "publisher", "op": "rm_rf", "kwargs": {}})["ok"] is False


def test_transport_accepts_replies_only_from_the_agent_it_asked(sandbox):
    from uagents import Agent
    foreman = Agent(name="foundry", seed="t")
    t = network.Transport(foreman, {"publisher": "agent1qpublisher"})
    on_result = foreman.handlers[RoleResult]
    rid = "r1"
    slot = {"event": threading.Event(), "res": None, "expect": "agent1qpublisher", "role": "publisher"}
    t.pending[rid] = slot
    ctx = Ctx(foreman)
    good = RoleResult(rid=rid, role="publisher", ok=True, payload=json.dumps({"result": "real"}))
    forged = RoleResult(rid=rid, role="publisher", ok=True, payload=json.dumps({"result": "forged"}))
    asyncio.run(on_result(ctx, "agent1qmallory", forged))           # right rid, wrong sender
    assert not slot["event"].is_set()
    asyncio.run(on_result(ctx, "agent1qpublisher", RoleResult(rid="nope", role="publisher", ok=True, payload="{}")))
    asyncio.run(on_result(ctx, "agent1qpublisher", good))
    assert slot["event"].is_set() and slot["res"]["result"] == "real"
    asyncio.run(on_result(ctx, "agent1qpublisher", forged))         # replay after completion
    assert slot["res"]["result"] == "real"
    assert [r["why"] for r in t.rejected] == ["unexpected sender", "unknown or completed request id",
                                             "duplicate result"]


# ------------------------------------------------------------------ payments

def _foreman_with_quoted_job(monkeypatch):
    from uagents import Agent
    from uagents_core.contrib.protocols.chat import ChatMessage, TextContent
    from foundry.agents import foreman as fm
    from foundry.agents import payments
    monkeypatch.setattr(config, "AGENTVERSE_API_KEY", None)
    monkeypatch.setattr(payments, "enabled", lambda: True)
    monkeypatch.setattr(payments, "create_checkout", lambda usd, jid, user, desc: {
        "checkout_session_id": f"cs_test_{jid}", "amount_cents": 50, "client_secret": "x"})
    verified = []
    monkeypatch.setattr(payments, "verify", lambda tx, jid, cents: verified.append(tx) or (True, "paid"))
    agent = Agent(name="foundry", seed="pay")
    f = fm.Foreman(agent)
    builds = []
    monkeypatch.setattr(f, "start_build", lambda ctx, sender, job: _record(builds, job))
    ctx = Ctx(agent)

    async def setup():
        for h in agent.events["startup"]:
            await h(ctx)
        await agent.handlers[ChatMessage](ctx, "alice", ChatMessage(
            timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
            content=[TextContent(type="text", text="Build an agent that reverses any text it gets, please")]))
    asyncio.run(setup())
    jid = next(iter(f.jobs))
    return f, agent, ctx, jid, builds, verified


async def _record(builds, job):
    builds.append(job["job_id"])


def test_payment_commit_from_another_user_is_rejected(sandbox, monkeypatch):
    from uagents_core.contrib.protocols.chat import ChatMessage, TextContent
    from uagents_core.contrib.protocols.payment import CommitPayment, Funds, RejectPayment
    f, agent, ctx, jid, builds, verified = _foreman_with_quoted_job(monkeypatch)

    async def go():
        await agent.handlers[ChatMessage](ctx, "alice", ChatMessage(
            timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
            content=[TextContent(type="text", text=json.dumps({"action": "approve_quote", "job_id": jid}))]))
        commit = CommitPayment(funds=Funds(currency="USD", amount="0.50", payment_method="stripe"),
                               recipient="foundry", transaction_id=f"cs_test_{jid}")
        await agent.handlers[CommitPayment](ctx, "agent1qmallory", commit)
        assert isinstance(ctx.sent[-1][1], RejectPayment) and builds == [] and verified == []
        await agent.handlers[CommitPayment](ctx, "alice", commit)
        await agent.handlers[CommitPayment](ctx, "alice", commit)    # client retry
        assert builds == [jid] and verified == [f"cs_test_{jid}"]  # verified once, built once
    asyncio.run(go())
    assert f.jobs[jid]["paid_tx"] == f"cs_test_{jid}"
    assert len(ledger.read("payment_completed")) == 1


def test_failed_payment_verification_prevents_the_build(sandbox, monkeypatch):
    from uagents_core.contrib.protocols.chat import ChatMessage, TextContent
    from uagents_core.contrib.protocols.payment import CommitPayment, Funds, RejectPayment
    from foundry.agents import payments
    f, agent, ctx, jid, builds, _ = _foreman_with_quoted_job(monkeypatch)
    monkeypatch.setattr(payments, "verify", lambda tx, jid, cents: (False, "payment not completed yet"))

    async def go():
        await agent.handlers[ChatMessage](ctx, "alice", ChatMessage(
            timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
            content=[TextContent(type="text", text=json.dumps({"action": "approve_quote", "job_id": jid}))]))
        await agent.handlers[CommitPayment](ctx, "alice", CommitPayment(
            funds=Funds(currency="USD", amount="0.50", payment_method="stripe"), recipient="foundry",
            transaction_id=f"cs_test_{jid}"))
    asyncio.run(go())
    assert isinstance(ctx.sent[-1][1], RejectPayment) and builds == []
    assert not f.jobs[jid].get("paid") and not ledger.read("payment_completed")
    assert ledger.read("payment_verification_failed")


def test_other_user_cannot_cancel_recheck_or_inspect(sandbox, monkeypatch):
    from uagents_core.contrib.protocols.chat import ChatMessage, TextContent
    f, agent, ctx, jid, builds, _ = _foreman_with_quoted_job(monkeypatch)

    async def go():
        for action in ("cancel", "recheck", "passport", "evidence", "publish"):
            await agent.handlers[ChatMessage](ctx, "agent1qmallory", ChatMessage(
                timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
                content=[TextContent(type="text", text=json.dumps({"action": action, "job_id": jid}))]))
            assert "belongs to another user" in _texts(ctx.sent[-1:])
    asyncio.run(go())
    assert f.jobs[jid]["status"] == "quoted"
