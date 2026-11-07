"""Regression tests for the audit findings: release gate, locked verifier, publish rollback,
authorization, single-use final set, credential-free sandbox."""
import asyncio
import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from foundry import bench, config, evolve, ledger, llm, pipeline, skills as sk
from foundry.harness import run as harness
from foundry.roles import publisher, scout

EVIL = ("import os\nKEY = os.environ.get('OPENAI_API_KEY')\nChatAcknowledgement\nagent.include(p)\n")


def _builder_returns(code):
    inner = llm.chat

    def chat(messages, *, provider, role, meter=None, **kw):
        if role in ("builder", "repair"):
            rec = {"role": role, "provider": provider, "model": "f", "fell_back": False, "tokens_in": 1,
                   "tokens_out": 1, "usd": 0.0, "t": 0}
            return {"text": f"```python name=main entry\n{code}```", **rec}
        return inner(messages, provider=provider, role=role, meter=meter, **kw)
    return chat


def test_static_violation_blocks_verified_even_if_all_tests_pass(sandbox, monkeypatch):
    monkeypatch.setattr(llm, "chat", _builder_returns(EVIL))
    monkeypatch.setattr(harness, "run", lambda agents, entry, cases, **kw: {
        "raw": {"cases": [], "load_errors": {}}, "stderr_tail": "",
        "results": [{"name": c["name"], "passed": True, "turns": []} for c in cases]})
    res = pipeline.build("an agent", sk.load(), budget_usd=1.0)
    assert res["verify"]["passed"] == res["verify"]["cases"]  # every executed test passed...
    assert res["release_gate"]["static_clean"] is False        # ...but the static gate failed
    assert res["status"] == "unverified"


def test_evolution_cannot_switch_off_its_own_verifier():
    base = sk.load(sk.BASELINE)
    assert sk.validate_patch({"kind": "config", "config_changes": {"tools": {"run_examiner_tests": False}}}, base)
    assert sk.validate_patch({"kind": "config", "config_changes": {"tools": {"preflight_static": False}}}, base)
    assert sk.validate_patch({"kind": "config", "config_changes": {"routing": {"examiner": "gpt"}}}, base)
    base.config.setdefault("tools", {})["preflight_static"] = False
    assert base.tool("preflight_static") and base.tool("run_examiner_tests")


class FakeDeploy:
    def __init__(self, reply):
        self.reply, self.stopped, self.n = reply, [], 0

    def create(self, name):
        self.n += 1
        return f"agent1qdeployed{self.n}"

    def upload(self, a, c): pass
    def update_profile(self, *a): return True
    def start(self, a): pass

    def stop(self, a):
        self.stopped.append(a)

    def invoke(self, target, message, wait_s=150):
        return {"texts": [self.reply], "cards": [], "acked": True, "latency_s": 5.0}


def _verified_job(sandbox):
    res = pipeline.build("an agent that reverses text", sk.load(), budget_usd=1.0)
    assert res["status"] == "verified"
    return res


def test_failed_live_check_rolls_back_release(sandbox):
    res = _verified_job(sandbox)
    case = {"name": "x", "turns": [{"send": "abc", "checks": [{"type": "text_contains", "value": "cba"}]}]}
    fake = FakeDeploy(reply="something else")
    out = publisher.publish(res, case, client=fake)
    assert out["released"] is False and fake.stopped == list(out["agents"].values())
    assert ledger.read("publish_failed") and not ledger.read("publish")
    assert scout.catalog() == []  # never offered for reuse


def test_publish_refuses_unverified(sandbox):
    with pytest.raises(ValueError):
        publisher.publish({"job_id": "job_000000", "status": "unverified", "request": "x"}, None, client=FakeDeploy(""))


def test_other_users_cannot_act_on_my_job(sandbox, monkeypatch):
    from uagents import Agent
    from uagents_core.contrib.protocols.chat import ChatMessage, TextContent
    from foundry.agents import foreman as fm
    from tests.test_foreman import Ctx, _cards, _texts
    monkeypatch.setattr(config, "AGENTVERSE_API_KEY", None)
    agent = Agent(name="foundry", seed="auth")
    f = fm.Foreman(agent)
    on_chat = agent.handlers[ChatMessage]

    async def scenario():
        ctx = Ctx(agent)
        for h in agent.events["startup"]:
            await h(ctx)

        async def say(who, text):
            await on_chat(ctx, who, ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
                                                content=[TextContent(type="text", text=text)]))
        await say("alice", "Build an agent that reverses any text it gets, please")
        jid = json.loads(_cards(ctx.sent)[-1]["card_payload"])["approve_cta"]["selection"]["job_id"]
        for action in ("approve_quote", "publish", "evidence", "learn"):
            await say("mallory", json.dumps({"action": action, "job_id": jid}))
            assert "belongs to another user" in _texts(ctx.sent[-1:])
        assert f.jobs[jid]["status"] == "quoted"
        await say("mallory", "evolve")
        assert "admin-only" in _texts(ctx.sent[-1:])
        await say("mallory", json.dumps({"action": "rollback", "version": "v0000"}))
        assert "admin-only" in _texts(ctx.sent[-1:])
        assert len(ledger.read("auth_denied")) == 6
    asyncio.run(scenario())


def test_final_set_is_single_use(sandbox):
    with pytest.raises(PermissionError):
        bench.load("final", purpose="peeking")
    evolve.evaluate_final(repeats=1, say=lambda *_: None)
    with pytest.raises(PermissionError):
        evolve.evaluate_final(repeats=1, say=lambda *_: None)
    evolve.evaluate_final(repeats=1, say=lambda *_: None, reopen_reason="judges asked for a rerun")
    assert ledger.read("final_reopened")[-1]["reason"] == "judges asked for a rerun"


def test_sandbox_gets_no_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-super-secret")
    monkeypatch.setenv("AGENTVERSE_API_KEY", "av-secret")
    env = harness._clean_env(str(tmp_path))
    assert "sk-super-secret" not in json.dumps(env) and "av-secret" not in json.dumps(env)
    cmd = harness._command("process", "/tmp/job.json", str(tmp_path), 60)
    assert cmd[1] == "-I" and cmd[2].endswith("runner.py")  # isolated mode, FOUNDRY not importable
    dcmd = harness._command("docker", "/tmp/job.json", str(tmp_path), 60)
    for flag in ("--network", "none", "--read-only", "--cap-drop", "ALL"):
        assert flag in dcmd


def test_job_file_is_readable_by_the_sandbox_user(monkeypatch):
    import os
    import stat
    seen = {}

    def fake_run(cmd, **kw):
        job = next(a for a in cmd if str(a).endswith("job.json") and not str(a).startswith("/sandbox"))
        seen["file"] = stat.S_IMODE(os.stat(job).st_mode)
        seen["dir"] = stat.S_IMODE(os.stat(os.path.dirname(job)).st_mode)
        seen["env"] = kw["env"]

        class P:
            stdout, stderr = 'FOUNDRY_RESULT:{"cases": [], "load_errors": {}}', ""
        return P()
    monkeypatch.setattr(harness.subprocess, "run", fake_run)
    monkeypatch.setattr(harness, "sandbox_mode", lambda: "process")
    harness.run([{"name": "main", "code": "x=1"}], "main", [{"name": "c", "turns": [{"send": "hi", "checks": []}]}])
    assert seen["file"] & 0o004 and seen["dir"] & 0o005  # world-readable so uid 10001 in Docker can read it
    assert "OPENAI_API_KEY" not in seen["env"]


def test_publish_smoke_test_needs_an_output_assertion():
    from foundry.roles import livecheck
    ack_only = {"name": "a", "turns": [{"send": "hi", "checks": [{"type": "ack"}]}]}
    real = {"name": "b", "turns": [{"send": "5 km", "checks": [{"type": "ack"}, {"type": "text_number", "value": 3.11}]}]}
    assert livecheck.pick_case([ack_only, real]) is real
    assert livecheck.pick_case([ack_only]) is None


def test_publish_without_a_live_test_is_not_released(sandbox):
    res = _verified_job(sandbox)
    fake = FakeDeploy(reply="anything")
    out = publisher.publish(res, None, client=fake)
    assert out["released"] is False and fake.stopped
