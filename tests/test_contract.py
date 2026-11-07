"""Issue 3 regression: the required topology comes from the USER's approved contract, not the Architect.

The contract is derived deterministically from the user's words (no model), validated, sealed with SHA-256,
shown on the capability card, approved by the user's tap, and enforced by the release gate. The Architect's plan
is only a fallback when the user asked for no multi-agent system."""
import asyncio
import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from foundry import bench, config, contract, ledger, llm, passport, pipeline, skills as sk
from tests.test_foreman import Ctx, _cards, _texts

THREE = ("Build THREE agents. 'verifier' checks a claim of the form '<a>+<b>=<c>' and answers CONFIRMED or REJECTED. "
         "'publisher' posts approved findings. 'investigator' is the entry agent: it asks verifier and replies.")
MULTI = ("Build a multi-agent system that accepts a GitHub issue, independently investigates whether the defect "
         "reproduces, and asks for my approval before publishing the findings.")


# ------------------------------------------------------------------ the contract itself

def test_contract_matches_every_dev_and_promotion_spec_and_has_no_false_positives():
    for split in ("dev", "promotion"):
        for sp in bench._load_dir(bench.DIRS[split]):
            c, t = contract.derive(sp["request"]), sp.get("topology")
            if t is None:
                assert c is None, sp["id"]                      # single-agent requests never become multi-agent
            else:
                assert sorted(c["agents"]) == sorted(t["agents"]) and c["min_agents"] == len(t["agents"]), sp["id"]
                assert sorted(c["must_receive"]) == sorted(t["must_receive"]), sp["id"]


def test_contract_agrees_with_the_final_set_without_revealing_it():
    """Read-only consistency check (no build, no ledger opening, nothing printed): final builds are unaffected."""
    disagreements = sum(1 for sp in bench._load_dir(bench.DIRS["final"])
                        if (contract.derive(sp["request"]) is None) != (sp.get("topology") is None))
    assert disagreements == 0


def test_contract_forms():
    c = contract.derive(MULTI)
    assert c["min_agents"] == 2 and c["agents"] == [] and "without a number" in c["notes"][0]
    c = contract.derive("At least: An Investigator Agent, An Independent Verification Agent, An Approval Agent")
    assert c["min_agents"] == 3
    c = contract.derive("Build an agent that reverses text", explicit_agents=["intake", "worker"])
    assert c["agents"] == ["intake", "worker"] and c["source"].startswith("agent names given")
    with pytest.raises(contract.ContractError):
        contract.derive("Build it", explicit_agents=["ok", "bad name!"])
    assert contract.derive("Build an agent that reverses any text it gets") is None


def test_sealed_contract_cannot_be_altered():
    c = contract.derive(THREE)
    contract.check_sealed(c)
    forged = {**c, "min_agents": 1}
    with pytest.raises(contract.ContractError):
        contract.check_sealed(forged)


# ------------------------------------------------------------------ the audit scenario, through the release gate

def _one_agent_architect_and_builder(inner):
    def chat(messages, *, provider, role, meter=None, **kw):
        if role == "architect":   # the Architect proposes ONE agent
            text = '{"agents":[{"name":"main","role":"does everything"}],"entry":"main"}'
        elif role in ("builder", "repair"):
            text = "```python name=main entry\n# approve_cta\nChatAcknowledgement\nagent.include(p)\n```"
        else:
            return inner(messages, provider=provider, role=role, meter=meter, **kw)
        if meter:
            meter.settle(meter.reserve(0.01), 0.01)
        return {"text": text, "role": role, "provider": provider, "model": "fake", "fell_back": False,
                "tokens_in": 1, "tokens_out": 1, "usd": 0.01, "t": 0}
    return chat


@pytest.mark.parametrize("request_text,min_agents", [(THREE, 3), (MULTI, 2)])
def test_user_asks_multi_agent_architect_proposes_one_build_is_not_verified(sandbox, monkeypatch, request_text,
                                                                            min_agents):
    monkeypatch.setattr(llm, "chat", _one_agent_architect_and_builder(llm.chat))
    res = pipeline.build(request_text, sk.load(), budget_usd=1.0)
    assert res["verify"]["passed"] == res["verify"]["cases"]          # every behavioural test passed...
    assert res["collaboration"]["status"] == "FAIL"                    # ...but NOT "NOT TESTED"
    assert res["status"] == "unverified"
    assert any(f"at least {min_agents} agents" in f["detail"] for f in res["collaboration"]["failures"])
    assert "Architect proposed 1 agent" in res["plan_conflict"]
    assert res["contract"]["min_agents"] == min_agents
    assert json.loads((config.RUNS / res["job_id"] / "contract.json").read_text())["sha256"] == res["contract"]["sha256"]
    assert ledger.read("build")[-1]["contract_sha256"] == res["contract"]["sha256"]


def test_tampered_contract_stops_the_build_before_any_model_call(sandbox):
    sealed = contract.derive(THREE)
    weakened = {**sealed, "must_receive": ["verifier"]}             # structurally valid, but not what was sealed
    invalid = {**sealed, "min_agents": 1}                            # structurally invalid
    before = sandbox["n"]
    for forged, why in ((weakened, "altered"), (invalid, "min_agents >= 2")):
        res = pipeline.build(THREE, sk.load(), budget_usd=1.0, contract=forged)
        assert res["status"] == "error" and why in res["error"], res["error"]
    assert sandbox["n"] == before                                    # no model call, nothing spent


def test_spec_topology_takes_precedence_over_the_contract(sandbox):
    spec = bench._load_dir(bench.DIRS["dev"])
    d10 = next(s for s in spec if s["id"] == "d10_upgrade_gate_multi")
    res = pipeline.build(d10["request"], sk.load(), budget_usd=1.0, topology=d10["topology"])
    assert res["collaboration"]["evidence"]["required"] == d10["topology"]


# ------------------------------------------------------------------ approved on the card, enforced as approved

def _foreman(monkeypatch):
    from uagents import Agent
    from uagents_core.contrib.protocols.chat import ChatMessage, TextContent
    from foundry.agents import foreman as fm
    monkeypatch.setattr(config, "AGENTVERSE_API_KEY", None)
    agent = Agent(name="foundry", seed="contract")
    f = fm.Foreman(agent)
    ctx = Ctx(agent)

    async def say(who, text):
        await agent.handlers[ChatMessage](ctx, who, ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
                                                                 content=[TextContent(type="text", text=text)]))
    return f, agent, ctx, say


def test_card_shows_contract_and_approval_records_its_hash(sandbox, monkeypatch):
    monkeypatch.setattr(llm, "chat", _one_agent_architect_and_builder(llm.chat))
    f, agent, ctx, say = _foreman(monkeypatch)

    async def go():
        for h in agent.events["startup"]:
            await h(ctx)
        await say("alice", json.dumps({"action": "submit_request", "request": MULTI, "agents": "intake, verifier, poster"}))
        card = _cards(ctx.sent)[-1]
        rows = {r["label"]: r["value"] for r in json.loads(card["card_payload"])["summary_rows"]}
        assert "at least 3 agents: intake, verifier, poster" in rows["Required architecture (your contract)"]
        jid = json.loads(card["card_payload"])["approve_cta"]["selection"]["job_id"]
        await say("alice", json.dumps({"action": "approve_quote", "job_id": jid}))
        for _ in range(200):
            await asyncio.sleep(0.05)
            if f.jobs[jid]["status"] not in ("building", "quoted"):
                break
        return jid
    jid = asyncio.run(go())
    job = f.jobs[jid]
    assert ledger.read("contract_approved")[-1]["contract_sha256"] == job["contract"]["sha256"]
    res = json.loads((config.RUNS / jid / "result.json").read_text())
    assert res["contract"]["sha256"] == job["contract_approval"]["sha256"]        # enforced what was approved
    assert res["status"] == "unverified" and res["collaboration"]["status"] == "FAIL"
    pp = json.loads((config.RUNS / jid / "passport.json").read_text())
    assert pp["contract"]["approved_by_user"] is True and pp["contract"]["min_agents"] == 3
    assert passport.validate(config.RUNS / jid / "passport.json") == []


def test_contract_changed_after_approval_is_refused(sandbox, monkeypatch):
    f, agent, ctx, say = _foreman(monkeypatch)

    async def go():
        for h in agent.events["startup"]:
            await h(ctx)
        await say("alice", THREE)
        jid = json.loads(_cards(ctx.sent)[-1]["card_payload"])["approve_cta"]["selection"]["job_id"]
        real_start = f.start_build

        async def tamper_then_start(c, s, job):
            job["contract"] = contract.seal({**job["contract"], "min_agents": 2, "agents": [], "entry": None,
                                             "must_receive": None})          # validly sealed, but NOT the approved one
            await real_start(c, s, job)
        f.start_build = tamper_then_start
        await say("alice", json.dumps({"action": "approve_quote", "job_id": jid}))
        for _ in range(100):
            await asyncio.sleep(0.05)
            if f.jobs[jid]["status"] == "error":
                break
        return jid
    jid = asyncio.run(go())
    assert f.jobs[jid]["status"] == "error" and "differs from the one you approved" in _texts(ctx.sent[-1:])
    assert not (config.RUNS / jid / "result.json").exists()


def test_invalid_agent_names_are_rejected_before_quoting(sandbox, monkeypatch):
    f, agent, ctx, say = _foreman(monkeypatch)

    async def go():
        await say("bob", json.dumps({"action": "submit_request", "request": MULTI, "agents": "a b, c"}))
    asyncio.run(go())
    assert "can't use those agent names" in _texts(ctx.sent[-1:]) and not f.jobs


# ------------------------------------------------------------------ passport enforces the contract too

def test_passport_rejects_verified_multi_agent_contract_without_collaboration(sandbox):
    res = pipeline.build("an agent that reverses text", sk.load(), budget_usd=1.0)
    path = config.RUNS / res["job_id"] / "passport.json"
    pp = json.loads(path.read_text())
    c = contract.derive(THREE)
    pp["contract"].update(multi_agent=True, min_agents=3, agents=c["agents"], sha256=c["sha256"], body=c)
    pp["integrity"]["body_sha256"] = passport.sha256_bytes(passport.canonical(
        {k: v for k, v in pp.items() if k != "integrity"}))
    path.write_text(json.dumps(pp))
    probs = " | ".join(passport.validate(path))
    assert "requires 3 agents and collaboration is NOT TESTED" in probs and "requires at least 3" in probs
