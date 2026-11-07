"""P0.1 regression tests: multi-agent collaboration FAILS CLOSED.

A multi-agent build can only be VERIFIED when the sandbox observed real agent-to-agent messages that
were PROCESSED by the receiving agent during a passing test. Missing observability = UNVERIFIED."""
import asyncio
import json

from foundry import llm, pipeline, skills as sk, topology
from foundry.harness import run as harness
from foundry.harness import runner

THREE = ["investigator", "verifier", "publisher"]
REQ = {"agents": THREE, "must_receive": ["verifier", "publisher"]}


def _recv(frm, to, t, ok=True, typ="Ask"):
    return {"from": frm, "to": to, "type": typ, "corr": f"c{t}", "t": t, "ok": ok}


def _send_for(r):
    """The send record the runner writes for the same message (same sender, receiver, correlation id)."""
    return {"from": r["from"], "to": r["to"], "type": r["type"], "corr": r["corr"], "t": r["t"] - 0.01}


def _raw(received, windows=((0.0, 10.0),), send_tap=True, recv_tap=True, sends=None):
    return {"traffic_tap": send_tap, "receive_tap": recv_tap, "received_log": received,
            "traffic_log": [_send_for(r) for r in received] if sends is None else sends,
            "case_windows": [list(w) for w in windows]}


PASSED = [{"name": "c0", "passed": True}]
REAL = [_recv("client", "investigator", 1.0, typ="ChatMessage"), _recv("investigator", "verifier", 1.5),
        _recv("verifier", "publisher", 2.0)]


def test_real_three_agent_collaboration_passes():
    out = topology.evaluate(_raw(REAL), THREE, "investigator", REQ, PASSED)
    assert out["status"] == "PASS", out
    assert out["evidence"]["peers"] == {"verifier": {"processed": 1, "matched": 1, "in_passing_test": 1},
                                        "publisher": {"processed": 1, "matched": 1, "in_passing_test": 1}}


def test_single_agent_pretending_to_be_three_fails():
    """One agent emits all the right outputs; the architecture it claims does not exist."""
    one = [_recv("client", "main", 1.0, typ="ChatMessage")]
    out = topology.evaluate(_raw(one), ["main"], "main", REQ, PASSED)
    assert out["status"] == "FAIL"
    assert any("were not built" in f["detail"] for f in out["failures"])


def test_three_named_agents_where_entry_does_all_the_work_fails():
    entry_only = [_recv("client", "investigator", 1.0, typ="ChatMessage")]
    out = topology.evaluate(_raw(entry_only), THREE, "investigator", REQ, PASSED)
    assert out["status"] == "FAIL" and len(out["failures"]) == 2


def test_sends_without_a_processing_handler_are_not_proof():
    """Peers were *sent* messages but no handler ever processed them (dead or crashing peer)."""
    raw = _raw([_recv("client", "investigator", 1.0, typ="ChatMessage")])
    raw["traffic_log"] += [{"from": "investigator", "to": "verifier", "type": "Ask", "corr": "x1", "t": 1.5},
                           {"from": "verifier", "to": "publisher", "type": "Ask", "corr": "x2", "t": 2.0}]
    assert topology.evaluate(raw, THREE, "investigator", REQ, PASSED)["status"] == "FAIL"
    crashing = REAL[:2] + [_recv("verifier", "publisher", 2.0, ok=False)]
    out = topology.evaluate(_raw(crashing), THREE, "investigator", REQ, PASSED)
    assert out["status"] == "FAIL"


def test_peer_used_only_during_a_failing_case_does_not_count():
    """Behaviour accepted by a PASSING test must depend on the peer, not some unrelated failed case."""
    windows = ((0.0, 3.0), (5.0, 8.0))
    results = [{"name": "ok", "passed": True}, {"name": "bad", "passed": False}]
    late = [_recv("client", "investigator", 1.0, typ="ChatMessage"), _recv("investigator", "verifier", 6.0),
            _recv("verifier", "publisher", 6.5)]
    assert topology.evaluate(_raw(late, windows), THREE, "investigator", REQ, results)["status"] == "FAIL"
    assert topology.evaluate(_raw(REAL, windows), THREE, "investigator", REQ, results)["status"] == "PASS"


def test_missing_observability_is_unverified_not_a_warning():
    out = topology.evaluate(_raw(REAL, recv_tap=False), THREE, "investigator", REQ, PASSED)
    assert out["status"] == "UNVERIFIED"
    assert out["failures"][0]["code"] == topology.TOPOLOGY_UNVERIFIED
    assert topology.check_spec(REQ, _raw(REAL, send_tap=False), THREE, "investigator", PASSED)  # hidden task unsolved


def test_single_agent_build_without_requirement_is_not_tested_and_allowed():
    out = topology.evaluate(_raw([]), ["main"], "main", None, PASSED)
    assert out["status"] == "NOT TESTED" and not out["failures"]


def test_plan_naming_three_agents_becomes_a_requirement():
    req = topology.required_from_plan({"agents": [{"name": n} for n in THREE], "entry": "investigator"}, ["main"])
    assert req == {"agents": THREE, "must_receive": ["verifier", "publisher"]}
    assert topology.required_from_plan({"agents": [{"name": "main"}]}, ["main"]) is None


# ------------------------------------------------------------------ issue 2: both sides must match

def _status(raw, results=PASSED):
    return topology.evaluate(raw, THREE, "investigator", REQ, results)


def test_receive_without_any_send_record_is_unverified_not_pass():
    out = _status(_raw(REAL, sends=[]))
    assert out["status"] == "UNVERIFIED"
    assert all(f["code"] == topology.TOPOLOGY_UNVERIFIED for f in out["failures"])
    assert out["evidence"]["peers"]["verifier"] == {"processed": 1, "matched": 0, "in_passing_test": 0}


def test_send_must_match_sender_receiver_and_correlation_id():
    good = [_send_for(r) for r in REAL]
    wrong_corr = [dict(s, corr="other") if s["to"] == "verifier" else s for s in good]
    wrong_sender = [dict(s, **{"from": "publisher"}) if s["to"] == "verifier" else s for s in good]
    wrong_receiver = [dict(s, to="publisher") if s["to"] == "verifier" and s["from"] == "investigator" else s
                      for s in good]
    no_corr = [{k: v for k, v in s.items() if k != "corr"} for s in good]
    for sends in (wrong_corr, wrong_sender, wrong_receiver, no_corr):
        assert _status(_raw(REAL, sends=sends))["status"] == "UNVERIFIED", sends
    assert _status(_raw(REAL, sends=good))["status"] == "PASS"


def test_failed_send_does_not_count_as_a_delivery():
    sends = [dict(_send_for(r), send_error="ConnectionError: refused") if r["to"] == "publisher" else _send_for(r)
             for r in REAL]
    assert _status(_raw(REAL, sends=sends))["status"] == "UNVERIFIED"


def test_matched_evidence_must_lie_in_a_passing_test_window():
    windows = ((0.0, 3.0), (5.0, 8.0))
    results = [{"name": "ok", "passed": True}, {"name": "bad", "passed": False}]
    sends = [dict(_send_for(r), t=6.0) if r["to"] == "verifier" else _send_for(r) for r in REAL]  # send in failing case
    assert _status(_raw(REAL, windows, sends=sends), results)["status"] == "FAIL"


def test_no_results_or_mismatched_windows_is_unverified():
    assert _status(_raw(REAL), results=None)["status"] == "UNVERIFIED"
    assert _status(_raw(REAL, windows=()), results=PASSED)["status"] == "UNVERIFIED"
    two = [{"name": "a", "passed": True}, {"name": "b", "passed": True}]
    assert _status(_raw(REAL, windows=((0.0, 10.0),)), results=two)["status"] == "UNVERIFIED"


def test_fail_outranks_unverified():
    """One peer silent (FAIL), another unconfirmed (UNVERIFIED): the verdict is FAIL."""
    recv = REAL[:2]
    out = _status(_raw(recv, sends=[]))
    assert out["status"] == "FAIL"
    assert {f["code"] for f in out["failures"]} == {"TOPOLOGY", topology.TOPOLOGY_UNVERIFIED}


# ------------------------------------------------------------------ end-to-end through the release gate

def _builder_and_plan(agents, plan_agents):
    inner = llm.chat

    def chat(messages, *, provider, role, meter=None, **kw):
        rec = {"role": role, "provider": provider, "model": "f", "fell_back": False, "tokens_in": 1,
               "tokens_out": 1, "usd": 0.0, "t": 0}
        if role in ("builder", "repair"):
            blocks = "".join(f"```python name={n}{' entry' if i == 0 else ''}\n# approve_cta\nChatAcknowledgement\n"
                             f"agent.include(p)\n```\n" for i, n in enumerate(agents))
            return {"text": blocks, **rec}
        if role == "architect":
            return {"text": json.dumps({"agents": [{"name": n} for n in plan_agents], "entry": plan_agents[0]}),
                    **rec}
        return inner(messages, provider=provider, role=role, meter=meter, **kw)
    return chat


def _runner(received, recv_tap=True):
    def run(agents, entry, cases, **kw):
        return {"raw": {"cases": [], "load_errors": {}, "traffic_tap": True, "receive_tap": recv_tap,
                        "received_log": received, "traffic_log": [_send_for(r) for r in received],
                        "case_windows": [[0, 10]] * len(cases)},
                "stderr_tail": "", "results": [{"name": c["name"], "passed": True, "turns": []} for c in cases]}
    return run


def test_release_gate_blocks_multi_agent_build_when_traffic_unobservable(sandbox, monkeypatch):
    monkeypatch.setattr(llm, "chat", _builder_and_plan(THREE, THREE))
    monkeypatch.setattr(harness, "run", _runner(REAL, recv_tap=False))
    res = pipeline.build("three agent system", sk.load(), budget_usd=1.0)
    assert res["verify"]["passed"] == res["verify"]["cases"]  # every behavioural test passed...
    assert res["release_gate"]["collaboration_status"] == "UNVERIFIED"
    assert res["status"] == "unverified"                       # ...but collaboration was never observed


def test_release_gate_blocks_one_agent_built_for_a_three_agent_design(sandbox, monkeypatch):
    monkeypatch.setattr(llm, "chat", _builder_and_plan(["main"], THREE))
    monkeypatch.setattr(harness, "run", _runner([_recv("client", "main", 1.0, typ="ChatMessage")]))
    res = pipeline.build("three agent system", sk.load(), budget_usd=1.0)
    assert res["status"] == "unverified" and res["collaboration"]["status"] == "FAIL"
    assert len(res["attempts"]) == 3  # the defect was fed to the repair loop each time


def test_release_gate_accepts_observed_collaboration(sandbox, monkeypatch):
    monkeypatch.setattr(llm, "chat", _builder_and_plan(THREE, THREE))
    monkeypatch.setattr(harness, "run", _runner(REAL))
    res = pipeline.build("three agent system", sk.load(), budget_usd=1.0)
    assert res["status"] == "verified", res.get("release_gate")
    assert res["collaboration"]["status"] == "PASS"


# ------------------------------------------------------------------ the receive tap itself (runner side)

def test_receive_tap_records_processing_and_handler_crashes():
    class Proto:
        def __init__(self):
            self.handlers = {}

        def on_message(self, model, replies=None):
            def deco(fn):
                self.handlers[model] = fn
                return fn
            return deco

    class Msg:
        def __init__(self, rid):
            self.rid = rid

    class Ctx:
        class agent:
            address = "agent1-verifier"

    received = []
    assert runner.install_receive_tap([Proto], received)
    assert not runner.install_receive_tap([Proto], received)  # idempotent: never double-wrapped
    p = Proto()

    @p.on_message(Msg)
    async def ok(ctx, sender, msg):
        return "done"

    q = Proto()

    @q.on_message(Msg)
    async def boom(ctx, sender, msg):
        raise ValueError("bad state")

    assert asyncio.run(p.handlers[Msg](Ctx(), "agent1-investigator", Msg("r1"))) == "done"
    try:
        asyncio.run(q.handlers[Msg](Ctx(), "agent1-investigator", Msg("r2")))
    except ValueError:
        pass
    assert [(r["from"], r["to"], r["ok"], r["corr"]) for r in received] == [
        ("agent1-investigator", "agent1-verifier", True, "rid:r1"),
        ("agent1-investigator", "agent1-verifier", False, "rid:r2")]
    assert "bad state" in received[1]["error"]
    assert p.handlers[Msg].__name__ == "ok"  # functools.wraps keeps the signature uAgents introspects


def test_taps_record_each_message_once_under_real_uagents_layering():
    """Real uAgents: Agent.on_message delegates to self._protocol.on_message, and ExternalContext.send calls
    super().send. Both layers are patched; each processed message and each send must be recorded ONCE."""
    import asyncio
    import importlib.util
    import sys
    import types
    from foundry.harness import runner as rn

    class Protocol:
        def __init__(self):
            self.handlers = []

        def on_message(self, model, replies=None, allow_unverified=False):
            def deco(fn):
                self.handlers.append(fn)
                return fn
            return deco

    class Agent:
        def __init__(self):
            self._protocol = Protocol()
            self.address = "agent1me"

        def on_message(self, model, replies=None, allow_unverified=False):
            return self._protocol.on_message(model, replies, allow_unverified)

    received = []
    assert rn.install_receive_tap([Protocol, Agent], received)
    a = Agent()

    @a.on_message(dict)
    async def h(ctx, sender, msg):
        return "ok"

    ctx = types.SimpleNamespace(agent=a)
    assert asyncio.run(a._protocol.handlers[0](ctx, "agent1peer", {"x": 1})) == "ok"
    assert len(received) == 1 and received[0]["ok"] and received[0]["from"] == "agent1peer"

    # send side: a fake uagents.context module with the real two-layer structure
    mod = types.ModuleType("uagents.context")

    class InternalContext:
        agent = a

        async def send(self, destination, message, *x, **k):
            return types.SimpleNamespace(status="delivered")

    class ExternalContext(InternalContext):
        async def send(self, destination, message, *x, **k):
            return await super().send(destination, message, *x, **k)
    mod.InternalContext, mod.ExternalContext = InternalContext, ExternalContext
    pkg = sys.modules.setdefault("uagents", types.ModuleType("uagents"))
    old = sys.modules.get("uagents.context")
    sys.modules["uagents.context"] = mod
    try:
        traffic = []
        assert rn._install_traffic_tap(traffic)
        asyncio.run(ExternalContext().send("agent1peer", {"y": 2}))
        asyncio.run(InternalContext().send("agent1peer", {"y": 3}))
        assert len(traffic) == 2 and all(t["delivery"] == "delivered" for t in traffic)
    finally:
        if old is not None:
            sys.modules["uagents.context"] = old
        else:
            sys.modules.pop("uagents.context", None)
