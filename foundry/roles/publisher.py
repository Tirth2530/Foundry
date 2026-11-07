"""Publisher: deploys a verified system to Agentverse as hosted agents, wires PEERS to the real
addresses, writes discoverable READMEs, independently invokes the live entry agent, and then checks
that the agent can actually be FOUND in the Agentverse search index.

Publication states (never collapsed into one "success"):
  FAILED        anything before the live behaviour check failed -> everything created is torn down
  DEPLOYED      running, profile written, live ACP call acknowledged AND behaviourally correct, but not
                (yet) returned by Agentverse search. The deployment is kept; `foundry recheck <job>` re-tests.
  DISCOVERABLE  all of the above AND the entry agent is returned by the marketplace search index.
Only DISCOVERABLE is reported as "published". Being reachable at a known address is not discoverability.

Each check is PASS / FAIL / UNVERIFIED / NOT TESTED. Progress is written to disk after every created
resource, so a crash mid-deploy leaves the exact addresses needed for cleanup (see jobs.recover)."""
from __future__ import annotations

import json
import time

from .. import agentverse, config, ledger
from . import livecheck

BADGES = ("![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)\n"
          "![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)\n")
PASS, FAIL, UNVERIFIED, NOT_TESTED = "PASS", "FAIL", "UNVERIFIED", "NOT TESTED"
DISCOVERY_WAIT_S = float(config.env("FOUNDRY_DISCOVERY_WAIT_S", "120"))


class PublishConflict(RuntimeError):
    pass


def readme(name: str, request: str, verify: dict, job_id: str, entry: bool) -> str:
    return (f"# {name}\n\n{BADGES}\nBuilt and verified by FOUNDRY (job `{job_id}`).\n\n"
            f"## What it does\n{request.strip()[:1200]}\n\n"
            f"## Evidence\n- Independent tests passed before publish: {verify.get('passed')}/{verify.get('cases')}\n"
            f"- {'Talk to this agent directly in ASI:One.' if entry else 'Internal peer of a FOUNDRY-built system.'}\n")


def secret_values() -> list[str]:
    """Every credential the FACTORY holds. None of them may ever appear in code we deploy."""
    vals = [config.AGENTVERSE_API_KEY, config.STRIPE_SECRET_KEY, config.env("CLAIM_AGENTVERSE_API_KEY"),
            config.env("GITHUB_TOKEN"), config.FOREMAN_SEED]
    vals += [p.get("api_key") for p in config.PROVIDERS.values()]
    return [v for v in vals if v and len(str(v)) >= 8]


def leaked_secrets(code: str) -> int:
    return sum(1 for v in secret_values() if v in code)


def _progress_path(jid):
    return config.RUNS / jid / "publish_progress.json"


def _progress(jid: str, **state) -> None:
    p = _progress_path(jid)
    p.parent.mkdir(parents=True, exist_ok=True)
    cur = json.loads(p.read_text()) if p.exists() else {}
    cur.update(state, updated=time.time())
    p.write_text(json.dumps(cur, indent=1, default=str))


def publish(job: dict, smoke_case: dict | None, client: agentverse.Client | None = None,
            discovery_wait_s: float | None = None) -> dict:
    if job.get("status") != "verified":
        raise ValueError(f"refusing to publish {job.get('job_id')}: status is {job.get('status')}, not verified")
    jid = job["job_id"]
    prev = json.loads(_progress_path(jid).read_text()) if _progress_path(jid).exists() else {}
    if prev.get("stage") in ("deploying", "verifying"):
        raise PublishConflict(f"a previous publish of {jid} was interrupted at '{prev['stage']}' with agents "
                              f"{prev.get('addresses')}; run `python -m foundry recover` to clean it up first")
    if prev.get("stage") == "kept":
        raise PublishConflict(f"{jid} is already deployed ({prev.get('entry')}); not deploying a duplicate")
    client = client or agentverse.Client()
    build = json.loads((config.RUNS / jid / "final" / "build.json").read_text())
    for a in build["agents"]:
        if leaked_secrets(a["code"]):
            raise ValueError(f"refusing to deploy {a['name']}: generated code contains one of the factory's "
                             "credentials")
    addrs: dict = {}
    steps: list = []
    checks = {"deployed": NOT_TESTED, "running": NOT_TESTED, "profile": NOT_TESTED, "acp_ack": NOT_TESTED,
              "behaviour": NOT_TESTED, "discoverable": NOT_TESTED}
    live: dict = {"skipped": True, "passed": False}
    discovery: dict = {}
    failure = None
    _progress(jid, stage="deploying", addresses={}, hosting_api=getattr(client, "api", None))
    try:
        for a in build["agents"]:
            addrs[a["name"]] = client.create(f"{a['name']}-{jid[-6:]}")
            _progress(jid, addresses=dict(addrs))  # crash here -> recover() knows what to delete
            steps.append({"created": a["name"], "address": addrs[a["name"]]})
        peers_line = "PEERS = " + json.dumps(addrs) + "\n"
        profiles = {}
        for a in build["agents"]:
            addr = addrs[a["name"]]
            client.upload(addr, peers_line + a["code"])
            profiles[a["name"]] = bool(client.update_profile(
                addr, f"{a['name']}-{jid[-6:]}",
                readme(a["name"], job["request"], job.get("verify", {}), jid, a["name"] == build["entry"]),
                job["request"][:180]))
            client.start(addr)
            steps.append({"started": a["name"], "profile_updated": profiles[a["name"]]})
        checks["deployed"] = PASS
        checks["profile"] = PASS if all(profiles.values()) else FAIL
        if checks["profile"] == FAIL:
            raise agentverse.AgentverseError(f"profile update failed for {[n for n, ok in profiles.items() if not ok]}"
                                             ": without a README the agent cannot be found or understood in ASI:One")
        not_running = [n for n, addr in addrs.items() if not client.wait_running(addr, timeout=90)]
        checks["running"] = FAIL if not_running else PASS
        if not_running:
            raise agentverse.AgentverseError(f"agents never reported running: {not_running}")
        _progress(jid, stage="verifying")
        if smoke_case:
            live = livecheck.run(client, addrs[build["entry"]], smoke_case)
        else:
            live = {"skipped": True, "passed": False, "checks": [{"type": "reply", "ok": False, "code": "NO_LIVE_TEST",
                                                                   "detail": "no independent test with an output check"}]}
        checks["acp_ack"] = PASS if live.get("acked") else (NOT_TESTED if live.get("skipped") else FAIL)
        checks["behaviour"] = PASS if live.get("passed") else (NOT_TESTED if live.get("skipped") else FAIL)
        if not live.get("passed"):
            failure = "live behaviour check failed" if not live.get("skipped") else "no live behaviour test available"
    except Exception as e:  # noqa: BLE001 - any failure must roll the deployment back
        failure = f"{type(e).__name__}: {e}"
        if checks["deployed"] == NOT_TESTED:
            checks["deployed"] = FAIL
        live.setdefault("checks", [{"type": "reply", "ok": False, "code": "DEPLOY_ERROR", "detail": str(e)[:300]}])
    released = failure is None
    entry_addr = addrs.get(build["entry"])
    if released:
        wait = DISCOVERY_WAIT_S if discovery_wait_s is None else discovery_wait_s
        discovery = check_discovery(client, entry_addr, f"{build['entry']}-{jid[-6:]}", job["request"], wait)
        checks["discoverable"] = {"PASS": PASS, "PENDING": UNVERIFIED}.get(discovery["status"], discovery["status"])
    cleanup = {} if released else client.teardown(list(addrs.values()))
    cleanup_confirmed = all(v.get("delete") == "ok" and v.get("absent") is True for v in cleanup.values()) \
        if cleanup else None
    status = "FAILED" if not released else ("DISCOVERABLE" if checks["discoverable"] == PASS else "DEPLOYED")
    out = {"job_id": jid, "publication_status": status, "published": status == "DISCOVERABLE",
           "agents": addrs, "entry": entry_addr, "released": released, "rolled_back": not released,
           "failure": failure, "checks": checks, "steps": steps, "cleanup": cleanup,
           "cleanup_confirmed": cleanup_confirmed,
           "cleanup_unconfirmed": [a for a, v in cleanup.items() if not (v.get("delete") == "ok" and v.get("absent"))],
           "hosting_api": getattr(client, "api", None), "discovery": discovery,
           "profile_url": f"https://agentverse.ai/agents/details/{entry_addr}/profile" if entry_addr else None,
           "ownership": "deployed under FOUNDRY's Agentverse account (see `foundry claim` to deploy into yours)",
           "live_invocation": live}
    (config.RUNS / jid / "publish.json").write_text(json.dumps(out, indent=1, default=str))
    _progress(jid, stage="kept" if released else ("rolled_back" if cleanup_confirmed else "cleanup_unconfirmed"),
              entry=entry_addr)
    ledger.append("publish" if released else "publish_failed", job_id=jid, agents=addrs, entry=entry_addr,
                  request=job["request"][:600], verify=job.get("verify"), failure=failure,
                  publication_status=status, checks=checks, cleanup_confirmed=cleanup_confirmed,
                  live_passed=live.get("passed"), live_latency_s=live.get("latency_s"))
    _reissue_passport(jid)
    return out


def _reissue_passport(jid: str) -> None:
    try:
        from .. import passport
        if (config.RUNS / jid / "result.json").exists():
            passport.write(jid)
    except Exception:  # noqa: BLE001 - the publish record above is authoritative; passport is derived
        pass


def check_discovery(client, address: str, name: str, request: str, wait_s: float) -> dict:
    fn = getattr(client, "discoverable", None)
    if fn is None:
        return {"status": "UNVERIFIED", "note": "client cannot search"}
    return fn(address, [name, address, request[:120]], wait_s=wait_s)


def recheck(job_id: str, client: agentverse.Client | None = None, wait_s: float = 0) -> dict:
    """Re-run ONLY the discoverability check for a deployed (kept) system and record the outcome."""
    p = config.RUNS / job_id / "publish.json"
    out = json.loads(p.read_text())
    if not out.get("released"):
        raise ValueError(f"{job_id} is not deployed (status {out.get('publication_status')})")
    client = client or agentverse.Client()
    build = json.loads((config.RUNS / job_id / "final" / "build.json").read_text())
    req = (config.RUNS / job_id / "request.txt").read_text() if (config.RUNS / job_id / "request.txt").exists() else ""
    d = check_discovery(client, out["entry"], f"{build['entry']}-{job_id[-6:]}", req, wait_s)
    out["discovery"] = d
    out["checks"]["discoverable"] = {"PASS": PASS, "PENDING": UNVERIFIED}.get(d["status"], d["status"])
    out["publication_status"] = "DISCOVERABLE" if out["checks"]["discoverable"] == PASS else "DEPLOYED"
    out["published"] = out["publication_status"] == "DISCOVERABLE"
    p.write_text(json.dumps(out, indent=1, default=str))
    ledger.append("publish_recheck", job_id=job_id, entry=out["entry"], publication_status=out["publication_status"],
                  discovery=d.get("status"))
    _reissue_passport(job_id)
    return out
