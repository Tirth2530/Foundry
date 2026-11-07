"""End-to-end trace: ASI:One intent -> roles -> sandbox -> release gate -> publisher -> live result."""
import json

from foundry import config, pipeline, skills as sk, trace
from foundry.roles import publisher
from tests.test_security import FakeDeploy


def test_build_and_publish_produce_one_connected_trace(sandbox, monkeypatch):
    monkeypatch.setattr(config, "AGENTVERSE_API_KEY", "av-secret-should-not-leak")
    res = pipeline.build("an agent that reverses text", sk.load(), budget_usd=1.0)
    case = {"name": "x", "turns": [{"send": "abc", "checks": [{"type": "text_contains", "value": "cba"}]}]}
    publisher.publish(res, case, client=FakeDeploy(reply="cba"), discovery_wait_s=0)
    spans = trace.read(res["job_id"])
    names = [s["name"] for s in spans]
    for n in ("build", "architect.plan", "examiner.write_tests", "builder.build", "integrator.check", "runner.run",
              "card_validator.audit", "release_gate", "publish", "live_invocation", "discovery"):
        assert n in names, names
    assert {s["trace_id"] for s in spans} == {res["job_id"]}
    root = next(s for s in spans if s["name"] == "build")
    children = {s["name"] for s in spans if s.get("parent") == root["span_id"]}
    assert {"architect.plan", "builder.build", "runner.run"} <= children     # nested under the build span
    runner_span = next(s for s in spans if s["name"] == "runner.run")
    assert runner_span["attrs"]["cases"] >= 1 and "passed" in runner_span["attrs"]
    assert "av-secret-should-not-leak" not in (config.RUNS / res["job_id"] / "trace.jsonl").read_text()
    assert "build" in trace.render(spans)


def test_failures_are_categorised_and_secrets_redacted():
    from foundry import llm
    from foundry.agentverse import AgentverseError
    e = AgentverseError("POST /v1/hosting -> HTTP 503 down")
    assert trace.error_category(e) == "provider_error"
    assert trace.error_category(llm.BudgetExceeded("x")) == "budget"
    assert trace.error_category(TimeoutError()) == "timeout"
    red = trace.redact({"api_key": "k", "nested": {"Authorization": "Bearer x", "ok": "fine"}})
    assert red == {"api_key": "[REDACTED]", "nested": {"Authorization": "[REDACTED]", "ok": "fine"}}
