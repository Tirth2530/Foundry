"""The three ASI:One cards (A capability proposal, B Proof Passport, C evolution verdict).
Every card must pass the validator FOUNDRY applies to others, and EVERY button must map to an action the
Foreman really handles: no decorative buttons."""
import asyncio
import json
from datetime import datetime, timezone
from uuid import uuid4

from foundry import cards, config, evolve, ledger, pipeline, skills as sk
from tests.test_foreman import Ctx, _cards, _confirm, _texts

HANDLED = {"approve_quote", "reject_quote", "publish", "evidence", "reuse", "build_new", "learn", "recheck", "cancel",
           "passport", "permissions", "reject_publication", "compare", "scoreboard", "status", "history", "evolve",
           "rollback", "welcome", "submit_request", "confirm_plan", "say"}


def _selections(md):
    p = json.loads(md["card_payload"])
    out = [c["selection"] for c in p.get("ctas", [])]
    out += [p[k]["selection"] for k in ("approve_cta", "reject_cta", "submit_cta") if k in p]
    out += [it["primary_cta"]["selection"] for it in p.get("items", [])]
    return out


def _assert_real_buttons(sent):
    for md in _cards(sent):
        assert cards.validate(md) == [], cards.validate(md)
        for sel in _selections(md):
            assert sel.get("action") in HANDLED, f"decorative button: {sel}"


def test_cards_a_and_b_drive_real_actions(sandbox, monkeypatch):
    from uagents import Agent
    from uagents_core.contrib.protocols.chat import ChatMessage, TextContent
    from foundry.agents import foreman as fm
    monkeypatch.setattr(config, "AGENTVERSE_API_KEY", None)
    agent = Agent(name="foundry", seed="cards")
    f = fm.Foreman(agent)
    on_chat = agent.handlers[ChatMessage]

    async def scenario():
        ctx = Ctx(agent)
        for h in agent.events["startup"]:
            await h(ctx)

        async def say(text):
            await on_chat(ctx, "alice", ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
                                                    content=[TextContent(type="text", text=text)]))
        await say("Build an agent that reverses any text it gets, please")
        await say(_confirm(ctx))
        a = _cards(ctx.sent)[-1]
        rows = {r["label"]: r["value"] for r in json.loads(a["card_payload"])["summary_rows"]}
        assert {"You want", "Existing agents", "Recommendation", "Estimated build cost", "Spending limit",
                "Permissions"} <= set(rows)
        jid = json.loads(a["card_payload"])["approve_cta"]["selection"]["job_id"]
        await say(f"permissions {jid}")
        assert "Code execution" in _cards(ctx.sent)[-1]["card_payload"]
        await say(json.dumps({"action": "approve_quote", "job_id": jid}))
        for _ in range(200):
            await asyncio.sleep(0.05)
            if f.jobs[jid]["status"] == "verified":
                break
        await asyncio.sleep(0.1)
        await say(json.dumps({"action": "passport", "job_id": jid}))
        b = _cards(ctx.sent)[-1]
        payload = b["card_payload"]
        assert "Proof Passport" in payload and "valid: digests match" in payload
        labels = [c["label"] for c in json.loads(payload)["ctas"]]
        assert {"Approve publication", "Reject publication", "Inspect evidence"} <= set(labels)
        await say(json.dumps({"action": "reject_publication", "job_id": jid}))
        assert "will not be published" in _texts(ctx.sent[-1:])
        assert ledger.read("publication_rejected")[-1]["job_id"] == jid
        _assert_real_buttons(ctx.sent)

    asyncio.run(scenario())


def test_card_c_evolution_verdict_and_compare(sandbox):
    from uagents import Agent
    from uagents_core.contrib.protocols.chat import ChatMessage, TextContent
    from foundry.agents import foreman as fm
    sandbox["strict"] = True
    res = pipeline.build("deployment approval agent with a review card", sk.load(), budget_usd=2.0)
    d = evolve.learn_from_job(res["job_id"], say=lambda *_: None)
    msg = fm.Foreman.verdict_message(d, "defects caught while building " + res["job_id"])
    md = [c.metadata for c in msg.content if getattr(c, "type", "") == "metadata"][0]
    rows = {r["label"] for r in json.loads(md["card_payload"])["summary_rows"]}
    assert {"Failure that triggered learning", "Proposed change", "Original version", "Candidate version",
            "Held-out comparison", "Transfer", "Regressions", "Equal budget", "Cost difference", "Decision"} <= rows
    agent = Agent(name="foundry", seed="cmp")
    fm.Foreman(agent)
    ctx = Ctx(agent)
    sent = [("u", msg)]
    _assert_real_buttons(sent)
    compare_sel = [s for s in _selections(md) if s["action"] == "compare"][0]

    async def go():
        for h in agent.events["startup"]:
            await h(ctx)
        await agent.handlers[ChatMessage](ctx, "bob", ChatMessage(
            timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
            content=[TextContent(type="text", text=json.dumps(compare_sel))]))
    asyncio.run(go())
    cmp_card = _cards(ctx.sent)[-1]
    assert cards.validate(cmp_card) == [] and "Lessons added" in cmp_card["card_payload"]
    assert "no (same models" in cmp_card["card_payload"]
