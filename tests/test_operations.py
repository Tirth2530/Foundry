"""P0.5 operational correctness: explicit sandbox, sealed sets need Docker, failing gates exit nonzero,
doctor resolves the Agentverse API exactly like deployment does."""
import json

import pytest

from foundry import agentverse, config, ledger, skills as sk
from foundry.harness import run as harness
from foundry.roles import judge


def test_untrusted_code_never_runs_without_an_explicit_sandbox_choice(monkeypatch):
    for value in (None, "", "auto", "yes"):
        if value is None:
            monkeypatch.delenv("FOUNDRY_SANDBOX", raising=False)
        else:
            monkeypatch.setenv("FOUNDRY_SANDBOX", value)
        with pytest.raises(harness.SandboxNotConfigured):
            harness.sandbox_mode()
    monkeypatch.setenv("FOUNDRY_SANDBOX", "docker")
    monkeypatch.setattr(harness.shutil, "which", lambda _: None)
    with pytest.raises(harness.SandboxNotConfigured, match="sandbox-build"):
        harness.sandbox_mode()                       # no silent downgrade from docker to process


def test_sealed_sets_require_docker_unless_a_logged_override(sandbox, monkeypatch):
    monkeypatch.delenv("FOUNDRY_ALLOW_PROCESS_SANDBOX_FOR_SEALED")
    with pytest.raises(PermissionError, match="docker"):
        judge.evaluate(sk.load(), purpose="peek", repeats=1)
    monkeypatch.setenv("FOUNDRY_ALLOW_PROCESS_SANDBOX_FOR_SEALED", "reviewer asked")
    judge.require_docker("promotion")
    assert ledger.read("sealed_without_docker")[-1]["reason"] == "reviewer asked"


def test_job_file_given_to_generated_code_contains_no_expectations(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        job = next(a for a in cmd if str(a).endswith("job.json"))
        seen["job"] = json.loads(open(job).read())

        class P:
            stdout, stderr = 'FOUNDRY_RESULT:{"cases": [], "load_errors": {}}', ""
        return P()
    monkeypatch.setattr(harness.subprocess, "run", fake_run)
    monkeypatch.setattr(harness, "sandbox_mode", lambda: "process")
    secret_case = {"name": "h", "turns": [{"send": "9781861972712",
                                           "checks": [{"type": "text_contains", "value": "HIDDEN-EXPECTED"}]}]}
    harness.run([{"name": "main", "code": "x=1"}], "main", [secret_case])
    assert "HIDDEN-EXPECTED" not in json.dumps(seen["job"]) and "checks" not in json.dumps(seen["job"])


def test_failing_selftest_exits_nonzero(sandbox, monkeypatch):
    from foundry import __main__ as cli
    monkeypatch.setattr(harness, "run", lambda agents, entry, cases, **kw: {
        "raw": {"cases": []}, "stderr_tail": "", "sandbox": "process",
        "results": [{"name": c["name"], "passed": False, "turns": []} for c in cases]})
    with pytest.raises(SystemExit) as e:
        cli.cmd_selftest(None)
    assert e.value.code == 1


def test_failing_gate1_exits_nonzero(monkeypatch, tmp_path):
    import runpy

    class Boom:
        api = "v1"

        def __init__(self, *a, **k):
            pass

        def create(self, name):
            raise agentverse.AgentverseError("HTTP 401 bad key")
    monkeypatch.setattr(config, "AGENTVERSE_API_KEY", "k")
    monkeypatch.setattr(agentverse, "Client", Boom)
    monkeypatch.setattr(config, "EVIDENCE", tmp_path)
    with pytest.raises(SystemExit) as e:
        runpy.run_path(str(config.ROOT / "tools" / "gate1_spike.py"), run_name="__main__")
    assert e.value.code == 1


def test_doctor_and_deploy_share_api_version_resolution(monkeypatch):
    calls = []

    def req(self, method, path, retries=2, **kw):
        calls.append((method, path))
        if path.startswith("/v1/"):
            e = agentverse.AgentverseError("HTTP 410 gone")
            e.status = 410
            raise e
        return {"address": "agent1qnew"} if method == "POST" else []
    monkeypatch.setattr(agentverse.Client, "_req", req)
    monkeypatch.setattr(agentverse.Client, "update_profile", lambda *a: True)
    doctor = agentverse.Client(api_key="k", api="auto")
    assert doctor.probe_api() == {"ok": True, "api": "v2", "tried": ["/v1/hosting/agents -> HTTP 410 gone",
                                                                     "/v2/hosting"]}
    deploy = agentverse.Client(api_key="k", api="auto")
    deploy.create("x")
    assert deploy.api == "v2" and deploy.ver["agent1qnew"] == "v2"
    forced = agentverse.Client(api_key="k", api="v1")
    assert forced.probe_api()["ok"] is False          # a forced version is never silently switched
