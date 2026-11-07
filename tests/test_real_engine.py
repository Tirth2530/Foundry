"""REAL engine tests: the actual uAgents runtime, the actual sandboxed runner, real ACP messages.
No scripted model is involved. Skipped automatically where uagents isn't installed (e.g. CI without it);
on your laptop after `pip install -r requirements.txt` these run for real."""
import json
import os

import pytest

import subprocess  # noqa: E402
import sys  # noqa: E402

if subprocess.run([sys.executable, "-I", "-c", "import uagents, uagents_core"], capture_output=True).returncode:
    pytest.skip("real uagents not installed for this interpreter", allow_module_level=True)

from foundry import config, static_check  # noqa: E402
from foundry.dsl import failures  # noqa: E402
from foundry.harness import run as harness  # noqa: E402

REF = config.ROOT / "tools" / "reference"
os.environ.setdefault("FOUNDRY_SANDBOX", "process")  # explicit choice; set FOUNDRY_SANDBOX=docker to test Docker


def test_runner_passes_correct_agent_and_catches_defective_one():
    spec = json.loads((REF / "spec.json").read_text())
    good = harness.run([{"name": "main", "code": (REF / "good_agent.py").read_text()}], "main", spec["tests"])
    assert all(r["passed"] for r in good["results"]), good["stderr_tail"]
    bad = harness.run([{"name": "main", "code": (REF / "bad_agent.py").read_text()}], "main", spec["tests"])
    codes = {f["code"] for f in failures(bad["results"])}
    assert "CARD_INVALID" in codes and "TEXT_MISMATCH" in codes


PROBE = (REF / "good_agent.py").read_text().replace(
    '    await reply(ctx, sender, "Usage: deploy <service> to <env>")',
    '    import os\n    await reply(ctx, sender, "ENV:" + str(os.environ.get("OPENAI_API_KEY")) + "|" + '
    'str(os.environ.get("AGENTVERSE_API_KEY")))')


def test_generated_code_cannot_see_api_keys(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-must-not-leak")
    monkeypatch.setenv("AGENTVERSE_API_KEY", "av-must-not-leak")
    assert static_check.check(PROBE)  # the static gate would block it anyway; we run it directly on purpose
    case = {"name": "probe", "turns": [{"send": "hello", "checks": [{"type": "text_contains", "value": "ENV:"}]}]}
    out = harness.run([{"name": "main", "code": PROBE}], "main", [case])
    text = out["results"][0]["turns"][0]["reply_text"]
    assert "ENV:None|None" in text and "must-not-leak" not in text


def test_docker_sandbox_cannot_read_sealed_tests():
    if harness.sandbox_mode() != "docker":
        pytest.skip("Docker sandbox not built (python -m foundry sandbox-build); process mode can read your files")
    target = str(next((config.ROOT / "bench" / "sealed" / "final").glob("*.json")))
    code = (REF / "good_agent.py").read_text().replace(
        '    await reply(ctx, sender, "Usage: deploy <service> to <env>")',
        f'    try:\n        data = open({target!r}).read()[:20]\n    except Exception as e:\n'
        f'        data = "BLOCKED " + type(e).__name__\n    await reply(ctx, sender, "READ:" + data)')
    case = {"name": "read", "turns": [{"send": "hello", "checks": []}]}
    out = harness.run([{"name": "main", "code": code}], "main", [case])
    assert "READ:BLOCKED" in out["results"][0]["turns"][0]["reply_text"]


def test_real_multi_agent_traffic_is_observed():
    """Two real agents in the sandbox: coordinator must hand off to 'upper'. Proves the traffic tap
    works on your uagents version, so TOPOLOGY checks are real, not assumed."""
    from foundry import topology
    agents = [{"name": "upper", "code": (REF / "multi_upper.py").read_text()},
              {"name": "coordinator", "code": (REF / "multi_coordinator.py").read_text()}]
    case = {"name": "m", "turns": [{"send": "hello agents", "checks": [{"type": "text_contains", "value": "UPPER: HELLO AGENTS"}]}]}
    out = harness.run(agents, "coordinator", [case])
    assert out["results"][0]["passed"], (out["results"], out["stderr_tail"])
    raw = out["raw"]
    assert raw.get("traffic_tap") is True, "traffic tap not installed on this uagents version"
    assert raw.get("receive_tap") is True, "receive tap not installed on this uagents version"
    req = {"agents": ["upper", "coordinator"], "must_receive": ["upper"]}
    assert topology.check_spec(req, raw, ["upper", "coordinator"], "coordinator", out["results"]) == []
    hand_off = [r for r in raw["received_log"] if r["from"] == "coordinator" and r["to"] == "upper"]
    assert hand_off and hand_off[0]["ok"] and hand_off[0]["corr"]
    # issue 2: the SAME correlation id must appear on the real send side (computed independently by the send tap)
    sent = [t for t in raw["traffic_log"] if t["from"] == "coordinator" and t["to"] == "upper"]
    assert any(t["corr"] == hand_off[0]["corr"] and not t.get("send_error") for t in sent), (sent, hand_off)
    assert topology.evaluate(raw, ["upper", "coordinator"], "coordinator", req, out["results"])["status"] == "PASS"
    # the same outputs from ONE agent must FAIL the collaboration check
    only_client = {**raw, "received_log": [r for r in raw["received_log"] if r["from"] == "client"]}
    assert topology.check_spec(req, only_client, ["upper", "coordinator"], "coordinator", out["results"])
