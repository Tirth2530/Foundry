"""REAL engine tests: the actual uAgents runtime, the actual sandboxed runner, real ACP messages.
No scripted model is involved. Skipped automatically where uagents isn't installed (e.g. CI without it);
on your laptop after `pip install -r requirements.txt` these run for real."""
import json
import os

import pytest

uagents = pytest.importorskip("uagents")
if "stubs" in (getattr(uagents, "__file__", "") or ""):
    pytest.skip("real uagents not installed", allow_module_level=True)

from foundry import config, static_check  # noqa: E402
from foundry.dsl import failures  # noqa: E402
from foundry.harness import run as harness  # noqa: E402

REF = config.ROOT / "tools" / "reference"


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
