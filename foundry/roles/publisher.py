"""Publisher: deploys a verified system to Agentverse as hosted agents, wires PEERS to the
real addresses, writes discoverable READMEs, then INDEPENDENTLY invokes the live entry agent
and re-checks its behaviour with the same test language. Nothing is called 'published'
unless the live call is observed."""
from __future__ import annotations

import json

from .. import agentverse, config, ledger
from . import livecheck

BADGES = ("![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)\n"
          "![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)\n")


def readme(name: str, request: str, verify: dict, job_id: str, entry: bool) -> str:
    return (f"# {name}\n\n{BADGES}\nBuilt and verified by FOUNDRY (job `{job_id}`).\n\n"
            f"## What it does\n{request.strip()[:1200]}\n\n"
            f"## Evidence\n- Independent tests passed before publish: {verify.get('passed')}/{verify.get('cases')}\n"
            f"- {'Talk to this agent directly in ASI:One.' if entry else 'Internal peer of a FOUNDRY-built system.'}\n")


def publish(job: dict, smoke_case: dict | None, client: agentverse.Client | None = None) -> dict:
    """Deploy every agent, wire PEERS, wait until each is running, then independently call the entry
    agent. ANY failure (exception mid-deploy, agent never running, live check failed) tears down
    everything this call created and records which cleanups were confirmed."""
    if job.get("status") != "verified":
        raise ValueError(f"refusing to publish {job.get('job_id')}: status is {job.get('status')}, not verified")
    client = client or agentverse.Client()
    build = json.loads((config.RUNS / job["job_id"] / "final" / "build.json").read_text())
    jid = job["job_id"]
    addrs: dict = {}
    steps: list = []
    live: dict = {"skipped": True, "passed": False}
    failure = None
    try:
        for a in build["agents"]:
            addrs[a["name"]] = client.create(f"{a['name']}-{jid[-6:]}")
            steps.append({"created": a["name"], "address": addrs[a["name"]]})
        peers_line = "PEERS = " + json.dumps(addrs) + "\n"
        for a in build["agents"]:
            addr = addrs[a["name"]]
            client.upload(addr, peers_line + a["code"])
            client.update_profile(addr, f"{a['name']}-{jid[-6:]}",
                                  readme(a["name"], job["request"], job.get("verify", {}), jid,
                                         a["name"] == build["entry"]), job["request"][:180])
            client.start(addr)
            steps.append({"started": a["name"]})
        not_running = [n for n, addr in addrs.items() if not client.wait_running(addr, timeout=90)]
        if not_running:
            raise agentverse.AgentverseError(f"agents never reported running: {not_running}")
        steps.append({"all_running": True})
        if smoke_case:
            live = livecheck.run(client, addrs[build["entry"]], smoke_case)
        else:
            live = {"skipped": True, "passed": False, "checks": [{"type": "reply", "ok": False, "code": "NO_LIVE_TEST",
                                                                   "detail": "no independent test with an output check"}]}
        if not live.get("passed"):
            failure = "live check failed"
    except Exception as e:  # noqa: BLE001 - any failure must roll the deployment back
        failure = f"{type(e).__name__}: {e}"
        live.setdefault("checks", [{"type": "reply", "ok": False, "code": "DEPLOY_ERROR", "detail": str(e)[:300]}])
    released = failure is None
    cleanup = {} if released else client.teardown(list(addrs.values()))
    entry_addr = addrs.get(build["entry"])
    out = {"job_id": jid, "agents": addrs, "entry": entry_addr, "released": released, "rolled_back": not released,
           "failure": failure, "steps": steps, "cleanup": cleanup,
           "cleanup_confirmed": all(v.get("delete") == "ok" for v in cleanup.values()) if cleanup else None,
           "hosting_api": getattr(client, "api", None),
           "profile_url": f"https://agentverse.ai/agents/details/{entry_addr}/profile" if entry_addr else None,
           "live_invocation": live}
    (config.RUNS / jid / "publish.json").write_text(json.dumps(out, indent=1, default=str))
    ledger.append("publish" if released else "publish_failed", job_id=jid, agents=addrs, entry=entry_addr,
                  request=job["request"][:600], verify=job.get("verify"), failure=failure,
                  cleanup_confirmed=out["cleanup_confirmed"], live_passed=live.get("passed"),
                  live_latency_s=live.get("latency_s"))
    return out
