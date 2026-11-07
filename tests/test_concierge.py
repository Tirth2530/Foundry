"""The conversational layer: FOUNDRY talks, clarifies and plans with the user; nothing is searched, charged or
built until the user confirms a plan card. The model is scripted; the Foreman, cards and memory are real."""
import asyncio
import json
from datetime import datetime, timezone
from uuid import uuid4

from foundry import cards, config, ledger
from foundry.agents import concierge, foreman as fm
from tests.test_foreman import Ctx, _cards, _texts
from uagents import Agent
from uagents_core.contrib.protocols.chat import ChatMessage, TextContent

PLAN = {"title": "ISBN-13 validator", "summary": "Checks ISBN-13 numbers and explains why one is invalid",
        "agents": [{"name": "isbn", "role": "validates"}], "input": "an ISBN-13, hyphens allowed",
        "output": "VALID or INVALID with the reason", "behaviours": ["strips hyphens/spaces"],
        "edge_cases": ["wrong length", "non-digits", "bad check digit"],
        "request": "Build an agent that validates ISBN-13 numbers, accepts hyphens and spaces, replies VALID or "
                   "INVALID with the reason (wrong length, non-digit, bad check digit)"}


def _run(monkeypatch, script):
    monkeypatch.setattr(config, "AGENTVERSE_API_KEY", None)
    seen = []

    def fake_respond(history, text, jobs):
        seen.append({"history": [dict(h) for h in history], "text": text})
        return concierge.parse(json.dumps(script.pop(0)))
    monkeypatch.setattr(concierge, "respond", fake_respond)
    agent = Agent(name="foundry", seed="c")
    f = fm.Foreman(agent)
    on_chat = agent.handlers[ChatMessage]
    ctx = Ctx(agent)

    async def say(text, who="u1"):
        await on_chat(ctx, who, ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
                                            content=[TextContent(type="text", text=text)]))
    return f, ctx, say, seen


def test_talk_clarify_plan_change_confirm(sandbox, monkeypatch):
    script = [
        {"intent": "greet", "reply": "Hey! What should your agent do?"},
        {"intent": "clarify", "reply": "Should it also fix a wrong check digit?", "options": ["Just validate", "Suggest the fix"]},
        {"intent": "plan", "reply": "Here's the plan.", "plan": PLAN},
        {"intent": "plan", "reply": "Updated: it also suggests the correct digit.",
         "plan": {**PLAN, "behaviours": ["strips hyphens/spaces", "suggests the correct check digit"]}},
    ]
    f, ctx, say, seen = _run(monkeypatch, script)

    async def go():
        await say("hey")
        assert _cards(ctx.sent)[-1]["card_kind"] == "carousel" and "What should your agent do" in _texts(ctx.sent[-1:])
        await say("I need something for ISBNs")
        q = _cards(ctx.sent)[-1]
        assert q["card_kind"] == "detail" and cards.validate(q) == []
        await say(json.dumps(json.loads(q["card_payload"])["ctas"][1]["selection"]))   # tap "Suggest the fix"
        assert seen[-1]["text"] == "Suggest the fix"                                    # a tap = typed words
        plan = _cards(ctx.sent)[-1]
        assert plan["card_kind"] == "review" and cards.validate(plan) == [] and "ISBN-13 validator" in plan["card_payload"]
        assert not f.jobs and not ledger.read("build")                                 # nothing started
        await say("also have it suggest the right digit")
        assert len(f.plans) == 2 and "suggests the correct check digit" in _cards(ctx.sent)[-1]["card_payload"]
        # the model saw the whole conversation, including what FOUNDRY showed
        h = " ".join(m["content"] for m in seen[-1]["history"])
        assert "I need something for ISBNs" in h and "Build plan: ISBN-13 validator" in h
        sel = json.loads(_cards(ctx.sent)[-1]["card_payload"])["approve_cta"]["selection"]
        await say(json.dumps(sel), who="mallory")                                       # not mallory's plan
        assert "another user" in _texts(ctx.sent[-1:]) and not f.jobs
        await say(json.dumps(sel))
        q = _cards(ctx.sent)[-1]
        assert q["card_kind"] == "review" and json.loads(q["card_payload"])["approve_cta"]["selection"]["action"] == "approve_quote"
        job = next(iter(f.jobs.values()))
        assert job["request"] == PLAN["request"] and ledger.read("plan_confirmed")
    asyncio.run(go())


def test_model_failure_never_starts_work(sandbox, monkeypatch):
    monkeypatch.setattr(config, "AGENTVERSE_API_KEY", None)

    def broken(*a, **k):
        raise concierge.ConciergeError("all providers down")
    monkeypatch.setattr(concierge, "respond", broken)
    agent = Agent(name="foundry", seed="d")
    f = fm.Foreman(agent)
    ctx = Ctx(agent)

    async def go():
        await agent.handlers[ChatMessage](ctx, "u", ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
                                          content=[TextContent(type="text", text="build an agent that reverses text")]))
    asyncio.run(go())
    assert _cards(ctx.sent)[-1]["card_kind"] == "review" and "Build plan" in _cards(ctx.sent)[-1]["card_payload"]
    assert not f.jobs


def test_parse_rejects_unusable_model_output():
    import pytest
    for bad in ('{"intent":"plan","reply":"x","plan":{"request":"short"}}', '{"intent":"dance"}', "hello",
                '{"intent":"chat","reply":""}'):
        with pytest.raises(concierge.ConciergeError):
            concierge.parse(bad)
    assert concierge.parse('{"intent":"command","command":"rm -rf","reply":"ok"}')["intent"] == "chat"


def test_scout_that_hangs_or_crashes_still_ends_in_a_quote(sandbox, monkeypatch):
    import time as _t
    from foundry import pipeline, skills as sk
    agent = Agent(name="foundry", seed="s")
    f = fm.Foreman(agent)
    out = []
    monkeypatch.setattr(f, "later", lambda to, msg: out.append(msg))
    monkeypatch.setattr(fm, "SCOUT_DEADLINE_S", 0.2)
    real = pipeline.rpc
    monkeypatch.setattr(pipeline, "rpc", lambda role, *a, **k: _t.sleep(2) if role == "scout" else real(role, *a, **k))
    f._scout_worker("u", "Build an agent that reverses any text it gets", None, sk.load())
    md = [dict(c.metadata) for m in out for c in m.content if getattr(c, "type", "") == "metadata"]
    assert md and md[-1]["card_kind"] == "review" and "did not finish" in json.dumps(md[-1])

    out.clear()
    monkeypatch.setattr(f, "_scout_body", lambda *a, **k: 1 / 0)                       # crashes
    f._scout_worker("u", "Build an agent that reverses any text it gets", None, sk.load())
    assert out and "approve_cta" in json.dumps([dict(c.metadata) for c in out[-1].content if getattr(c, "type", "") == "metadata"])
