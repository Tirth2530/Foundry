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
    if job.get("status") != "verified":
        raise ValueError(f"refusing to publish {job.get('job_id')}: status is {job.get('status')}, not verified")
    client = client or agentverse.Client()
    build = json.loads((config.RUNS / job["job_id"] / "final" / "build.json").read_text())
    addrs = {a["name"]: client.create(f"{a['name']}-{job['job_id'][-6:]}") for a in build["agents"]}
    peers_line = "PEERS = " + json.dumps(addrs) + "\n"
    profile = {}
    for a in build["agents"]:
        is_entry = a["name"] == build["entry"]
        client.upload(addrs[a["name"]], peers_line + a["code"])
        profile[a["name"]] = client.update_profile(
            addrs[a["name"]], f"{a['name']}-{job['job_id'][-6:]}",
            readme(a["name"], job["request"], job.get("verify", {}), job["job_id"], is_entry),
            job["request"][:180])
        client.start(addrs[a["name"]])
    entry_addr = addrs[build["entry"]]
    live = livecheck.run(client, entry_addr, smoke_case)
    released = bool(live.get("passed"))
    if not released:
        # RELEASE GATE: a deployment whose live behaviour wasn't confirmed is taken down again
        for addr in addrs.values():
            try:
                client.stop(addr)
            except agentverse.AgentverseError:
                pass
    out = {"job_id": job["job_id"], "agents": addrs, "entry": entry_addr, "readme_set": profile,
           "released": released, "rolled_back": not released,
           "profile_url": f"https://agentverse.ai/agents/details/{entry_addr}/profile", "live_invocation": live}
    (config.RUNS / job["job_id"] / "publish.json").write_text(json.dumps(out, indent=1))
    ledger.append("publish" if released else "publish_failed", job_id=job["job_id"], agents=addrs, entry=entry_addr, request=job["request"][:600],
                  verify=job.get("verify"),
                  live_passed=live.get("passed"), live_latency_s=live.get("latency_s"))
    return out
