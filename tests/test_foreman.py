"""The ASI:One conversation end to end (direct mode, scripted model): welcome form ->
quote review card -> approve -> background build -> verified detail card -> evidence ->
history. Every card the Foreman sends must pass the same validator it applies to others."""
import asyncio
import json
import time
from datetime import datetime, timezone
from uuid import uuid4

from foundry import cards, config
from foundry.agents import foreman as fm
from uagents import Agent
from uagents_core.contrib.protocols.chat import ChatMessage, TextContent


class Ctx:
    def __init__(self, agent):
        self.agent = agent
        self.sent = []
        self.logger = type("L", (), {"info": print, "exception": print, "warning": print})()

    async def send(self, to, msg):
        self.sent.append((to, msg))


def _cards(msgs):
    out = []
    for _, m in msgs:
        for c in getattr(m, "content", []):
            if getattr(c, "type", "") == "metadata":
                out.append(dict(c.metadata))
    return out


def _texts(msgs):
    return " ".join(c.text for _, m in msgs for c in getattr(m, "content", []) if getattr(c, "type", "") == "text")


def test_conversation(sandbox, monkeypatch):
    monkeypatch.setattr(config, "AGENTVERSE_API_KEY", None)
    agent = Agent(name="foundry", seed="t")
    f = fm.Foreman(agent)
    on_chat = agent.handlers[ChatMessage]

    async def scenario():
        ctx = Ctx(agent)
        for h in agent.events["startup"]:
            await h(ctx)

        async def say(text):
            await on_chat(ctx, "user1", ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
                                                    content=[TextContent(type="text", text=text)]))

        await say("hi")
        md = _cards(ctx.sent)[-1]
        assert md["card_kind"] == "form" and cards.validate(md) == []

        await say(json.dumps({"action": "submit_request", "request": "Build an agent that reverses any text it gets",
                              "budget": "1.0"}))
        md = _cards(ctx.sent)[-1]
        assert md["card_kind"] == "review" and cards.validate(md) == []
        jid = json.loads(md["card_payload"])["approve_cta"]["selection"]["job_id"]

        await say(f"The user clicked Approve & build for {jid}")  # planner-mediated prose path
        for _ in range(200):
            await asyncio.sleep(0.05)
            if any(c["card_kind"] == "detail" for c in _cards(ctx.sent)):
                break
        detail = [c for c in _cards(ctx.sent) if c["card_kind"] == "detail"][-1]
        assert cards.validate(detail) == [], cards.validate(detail)
        assert "Verified" in json.loads(detail["card_payload"])["title"]
        assert f.jobs[jid]["status"] == "verified"

        await say(json.dumps({"action": "approve_quote", "job_id": jid}))  # duplicate click is idempotent
        assert "already verified" in _texts(ctx.sent[-1:])

        await say(json.dumps({"action": "evidence", "job_id": jid}))
        ev = _cards(ctx.sent)[-1]
        assert ev["card_kind"] == "detail" and cards.validate(ev) == []

        await say(json.dumps({"action": "publish", "job_id": jid}))
        assert "AGENTVERSE_API_KEY" in _texts(ctx.sent[-1:])

        await say("history")
        assert "evolution" in _texts(ctx.sent[-1:]).lower()

        await say("status")
        st = _cards(ctx.sent)[-1]
        assert st["card_kind"] == "carousel" and cards.validate(st) == []

    asyncio.run(scenario())


def test_pipeline_over_the_agent_network_wire(sandbox, monkeypatch):
    """Every role call goes through the same JSON payloads the RoleTask/RoleResult messages carry."""
    from foundry import pipeline, skills as sk
    from foundry.agents import network
    hops = []

    def wire(role, payload):
        task = json.loads(json.dumps(payload, default=str))
        task["role"] = role
        hops.append(f"{role}.{task['op']}")
        res = network.execute(task)
        return json.loads(json.dumps(res, default=str))

    monkeypatch.setattr(pipeline, "TRANSPORT", wire)
    res = pipeline.build("an agent that does a thing", sk.load(), budget_usd=1.0)
    assert res["status"] == "verified" and res["cost"]["total_usd"] > 0
    for hop in ("architect.plan", "examiner.write_tests", "adversary.cases", "builder.build",
                "integrator.check", "runner.run", "card_validator.audit", "ledger.append"):
        assert hop in hops, hops
    assert network._recovery_decide("builder", "budget cap", 1)["action"] == "fail"
    assert network._recovery_decide("builder", "timeout", 1)["action"] == "retry"


def test_conversation_reuse_path(sandbox, monkeypatch):
    from foundry.roles import scout
    monkeypatch.setattr(config, "AGENTVERSE_API_KEY", "x")
    found = {"found": True, "candidate": {"id": "foundry:job_abc123", "source": "foundry_catalog",
                                          "address": "agent1qcatalog", "job_id": "job_abc123"},
             "live": {"passed": True, "basis": "its original independent test", "latency_s": 8.0, "texts": ["VALID"]},
             "match": {"confidence": 0.92, "reason": "same task", "gaps": []},
             "search": {"queries": ["isbn checker"], "raw_hits": 9, "kept": 3, "errors": []},
             "search_note": "4 queries, 9 hits, 3 live chat agents kept",
             "audits": [
                 {"candidate": {"address": "agent1qliar", "name": "isbn-pro", "source": "agentverse_search",
                                "platform": {"recent_success_rate": 0.99}},
                  "need": {"passed": False, "texts": ["jokes only"]}, "claim": {"passed": False},
                  "need_basis": "a test written from your request", "claim_text": "checks ISBNs"},
                 {"candidate": {"address": "agent1qcatalog", "name": "FOUNDRY job_abc123", "source": "foundry_catalog"},
                  "need": {"passed": True, "texts": ["VALID"], "latency_s": 8.0}, "claim": None,
                  "need_basis": "its original independent test"}]}
    monkeypatch.setattr(scout, "find", lambda **kw: found)
    agent = Agent(name="foundry", seed="t2")
    fm.Foreman(agent)
    on_chat = agent.handlers[ChatMessage]

    async def scenario():
        ctx = Ctx(agent)
        for h in agent.events["startup"]:
            await h(ctx)

        async def say(text):
            await on_chat(ctx, "user2", ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
                                                    content=[TextContent(type="text", text=text)]))
        await say("Build an ISBN-13 checker agent that replies VALID or INVALID")
        assert "Scout" in _texts(ctx.sent)
        for _ in range(100):
            await asyncio.sleep(0.05)
            if any(c["card_kind"] == "review" for c in _cards(ctx.sent)):
                break
        offer = [c for c in _cards(ctx.sent) if c["card_kind"] == "review"][-1]
        assert cards.validate(offer) == [] and "already does this" in offer["card_payload"]
        jid = json.loads(offer["card_payload"])["approve_cta"]["selection"]["job_id"]
        audit = [c for c in _cards(ctx.sent) if c["card_kind"] == "carousel"][-1]
        assert cards.validate(audit) == [] and "failed live check" in audit["card_payload"]
        await say(json.dumps({"action": "reuse", "job_id": jid, "pick": "1"}))
        done = _cards(ctx.sent)[-1]
        assert done["card_kind"] == "detail" and cards.validate(done) == [] and "agent1qcatalog" in done["card_payload"]
        await say(json.dumps({"action": "reuse", "job_id": jid}))
        assert "reused" in _texts(ctx.sent[-1:])

    asyncio.run(scenario())
