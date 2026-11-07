"""FOUNDRY Proof Passport: one machine-readable, independently checkable record of what was verified about a
generated system, and how. It is DERIVED from the existing evidence (the job's run folder and the hash-chained
ledger); it is not a second evidence system.

Every claim carries a status (PASS · FAIL · UNVERIFIED · NOT TESTED) and a kind:
  OBSERVED  an executed check or an externally confirmed event, with the artifact that shows it
  INFERRED  a model's explanation, plan or prediction: never counted as verification

`python -m foundry passport-verify <passport.json>` re-derives every digest from the files on disk and the
ledger, and re-checks the verdict's consistency rules. It needs no model and no network, and is deterministic.
A passing test suite is evidence about the executed inputs, not proof of universal correctness; the passport
says so in `limitations`.
"""
from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import time
from pathlib import Path

from . import config, ledger

SCHEMA = "foundry.passport/1"
STATUSES = ("PASS", "FAIL", "UNVERIFIED", "NOT TESTED")
KINDS = ("OBSERVED", "INFERRED")
HARNESS_FILES = ("foundry/harness/runner.py", "foundry/harness/run.py", "foundry/dsl.py", "foundry/topology.py",
                 "foundry/cards.py", "foundry/static_check.py")
REQUIRED = ("schema", "passport_id", "build", "spec", "contract", "code", "skills", "models", "harness", "environment",
            "examiner", "tests", "collaboration", "security", "cards", "external_actions", "publication", "spend",
            "claims", "limitations", "untested_assumptions", "evidence", "experiments", "ledger_anchor", "verdict",
            "integrity")


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    return sha256_bytes(p.read_bytes())


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()


def _dir_digest(root: Path, pattern: str = "**/*") -> str:
    h = hashlib.sha256()
    for p in sorted(x for x in root.glob(pattern) if x.is_file()):
        h.update(str(p.relative_to(root)).encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def code_digest(job_dir: Path) -> dict:
    files = sorted((job_dir / "final" / "agents").glob("*.py"))
    per = {p.stem: sha256_file(p) for p in files}
    return {"agents": per, "system_sha256": sha256_bytes(canonical(per))}


def harness_digest() -> str:
    h = hashlib.sha256()
    for rel in HARNESS_FILES:
        p = config.ROOT / rel
        h.update(rel.encode())
        h.update(p.read_bytes() if p.exists() else b"<missing>")
    return h.hexdigest()


def _git_revision() -> str | None:
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=config.ROOT, capture_output=True, text=True, timeout=5)
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "foundry"], cwd=config.ROOT,
                               capture_output=True, text=True, timeout=5).stdout.strip()
        return (r.stdout.strip() + ("+dirty" if dirty else "")) if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(config.RUNS))
    except ValueError:
        return str(p)


def _last_attempt_verify(job_dir: Path) -> Path | None:
    vs = sorted((job_dir / "attempts").glob("*/verify.json"))
    return vs[-1] if vs else None


def _ledger_anchor(job_id: str) -> dict:
    recs = ledger.read()
    for i in range(len(recs) - 1, -1, -1):
        r = recs[i]
        if r.get("kind") == "build" and r.get("job_id") == job_id:
            return {"kind": "build", "index": i, "hash": r["hash"], "t": r["t"]}
    return {"kind": "build", "index": None, "hash": None, "note": "no ledger record found for this build"}


def _st(ok) -> str:
    return "PASS" if ok is True else "FAIL" if ok is False else "NOT TESTED"


def build_passport(job_id: str, job_dir: Path | None = None) -> dict:
    jd = job_dir or (config.RUNS / job_id)
    res = json.loads((jd / "result.json").read_text())
    request = (jd / "request.txt").read_text() if (jd / "request.txt").exists() else res.get("request", "")
    gate = res.get("release_gate") or {}
    v = res.get("verify") or {}
    collab = res.get("collaboration") or {"status": "NOT TESTED", "evidence": {}}
    last = (res.get("attempts") or [{}])[-1]
    publish = json.loads((jd / "publish.json").read_text()) if (jd / "publish.json").exists() else None
    payments = [r for r in ledger.read() if r.get("job_id") == job_id and r["kind"].startswith("payment")]
    exps = [{"candidate": d["candidate"], "decision": d["decision"], "reason": d["reason"],
             "parent": d["parent"], "new_version": d.get("new_version")}
            for d in ledger.read("patch_decision") if d.get("origin") == f"live build {job_id}"]
    vfile = _last_attempt_verify(jd)
    cards_emitted = 0
    if vfile:
        cards_emitted = sum(t.get("cards", 0) for r in json.loads(vfile.read_text()).get("results", [])
                            for t in r.get("turns", []))
    card_problems = last.get("card_audit") or []
    cards_status = "FAIL" if card_problems or last.get("skipped_run") else ("PASS" if cards_emitted else "NOT TESTED")
    static_status = _st(gate.get("static_clean")) if gate else "NOT TESTED"
    tests_status = "PASS" if gate.get("all_tests_passed") else ("FAIL" if v.get("cases") else "NOT TESTED")
    cost = res.get("cost") or {}

    # ---------------------------------------------------------------- external actions & publication
    live = (publish or {}).get("live_invocation") or {}
    receipts = []
    if publish and not live.get("skipped"):
        receipts.append({"action": "independent live ACP invocation of the deployed entry agent",
                         "status": "PASS" if live.get("passed") else "FAIL", "kind": "OBSERVED",
                         "target": publish.get("entry"), "relay": live.get("relay"),
                         "latency_s": live.get("latency_s"), "evidence": _rel(jd / "publish.json")})
    for pr in payments:
        receipts.append({"action": pr["kind"], "status": "PASS" if pr["kind"] == "payment_completed" else "FAIL",
                         "kind": "OBSERVED", "evidence": f"ledger#{pr['hash']}",
                         "verified_with": pr.get("verified_with")})
    pub_checks = (publish or {}).get("checks") or {}
    publication = {"status": (publish or {}).get("publication_status", "NOT PUBLISHED"),
                   "checks": pub_checks, "entry_address": (publish or {}).get("entry"),
                   "agents": (publish or {}).get("agents"),
                   "discoverable": pub_checks.get("discoverable", "NOT TESTED"),
                   "discovery": {k: (publish or {}).get("discovery", {}).get(k) for k in ("status", "found_by", "after_s")}
                   if publish else None,
                   "ownership": (publish or {}).get("ownership"),
                   "cleanup_confirmed": (publish or {}).get("cleanup_confirmed")}

    # ---------------------------------------------------------------- claims
    def claim(text, status, kind, evidence=None):
        return {"claim": text, "status": status, "kind": kind, "evidence": evidence}
    vrel = _rel(vfile) if vfile else None
    claims = [
        claim(f"{v.get('passed', 0)}/{v.get('cases', 0)} independent test cases passed over ACP in the sandbox",
              tests_status, "OBSERVED", vrel),
        claim("tests were written from the request only by a different model family than the builder",
              "PASS" if (res.get("examiner") or {}).get("independent") else "FAIL", "OBSERVED",
              _rel(jd / "examiner_tests.json")),
        claim("required inter-agent messages were delivered AND processed during passing tests",
              collab.get("status", "NOT TESTED"), "OBSERVED", vrel),
        claim("hosted-contract static gate (no files, env, processes, dynamic code) is clean", static_status,
              "OBSERVED", _rel(jd / "result.json")),
        claim("every emitted Interactive Card validates against the ASI:One schema", cards_status, "OBSERVED", vrel),
        claim("the system design follows the Architect plan", "NOT TESTED", "INFERRED", _rel(jd / "plan.json")),
        claim("behaviour on inputs outside the executed tests is correct", "NOT TESTED", "INFERRED"),
    ]
    if publish:
        claims.append(claim("deployed agent answered an independent live call correctly",
                            pub_checks.get("behaviour", "NOT TESTED"), "OBSERVED", _rel(jd / "publish.json")))
        claims.append(claim("entry agent is returned by the Agentverse search index",
                            pub_checks.get("discoverable", "NOT TESTED"), "OBSERVED", _rel(jd / "publish.json")))

    limitations = [
        "passing tests show correct behaviour on the executed inputs only; they are not a proof of correctness",
        "model spend is metered per call; Agentverse hosting and local sandbox compute are not metered",
        "the ledger is hash-chained (edits to earlier records are detectable) but not signed: whoever can rewrite "
        "the whole file can rebuild the chain",
    ]
    if collab.get("status") == "NOT TESTED":
        limitations.append("single-agent system: no collaboration requirement")
    if res.get("sandbox") == "process":
        limitations.append("tests ran in the weaker 'process' sandbox, not Docker")
    untested = ["third-party agents or services the system may call at runtime",
                "load, concurrency and long-running behaviour beyond the executed cases"]
    if not publish:
        untested.append("behaviour on Agentverse hosting (not published)")
    elif pub_checks.get("discoverable") != "PASS":
        untested.append("discovery by an ASI:One user (search index has not returned the agent)")

    artifacts = [p for p in (jd / "request.txt", jd / "plan.json", jd / "examiner_tests.json",
                             jd / "adversary_tests.json", jd / "result.json", jd / "final" / "build.json",
                             vfile, jd / "publish.json") if p and p.exists()]
    models = sorted({(c.get("provider"), c.get("model")) for c in cost.get("models", [])}) or \
        sorted({(r.get("provider"), None) for r in (cost.get("by_role") or {}).values()})
    verdict = "VERIFIED" if res.get("status") == "verified" else res.get("status", "unknown").upper()

    body = {
        "schema": SCHEMA,
        "passport_id": f"FP-{job_id}-{code_digest(jd)['system_sha256'][:10]}",
        "build": {"job_id": job_id, "status": res.get("status"), "seconds": res.get("seconds"),
                  "attempts": len(res.get("attempts") or []), "foundry_revision": _git_revision(),
                  "issued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
        "spec": {"request_sha256": sha256_bytes(request.encode()), "request_excerpt": request[:240]},
        "contract": _contract_section(res.get("contract"), jd),
        "code": {**code_digest(jd), "entry": (res.get("artifact") or {}).get("entry"),
                 "agent_count": len((res.get("artifact") or {}).get("agents") or [])},
        "skills": {"version": res.get("skills_version"), "digest": _skills_digest(res.get("skills_version"))},
        "models": [{"provider": p, "model": m} for p, m in models],
        "harness": {"sha256": harness_digest(), "files": list(HARNESS_FILES)},
        "environment": {"python": platform.python_version(), "platform": platform.platform(terse=True),
                        "sandbox": res.get("sandbox"), "uagents": (res.get("runtime") or {}).get("uagents")},
        "examiner": {"provider": (res.get("examiner") or {}).get("provider"),
                     "independent_family": (res.get("examiner") or {}).get("independent"),
                     "cases": (res.get("examiner") or {}).get("cases"), "sees_code": False,
                     "certifies_itself": False},
        "tests": {"status": tests_status, "executed": v.get("cases", 0), "passed": v.get("passed", 0),
                  "examiner_passed": gate.get("examiner_tests_passed")},
        "collaboration": {"status": collab.get("status"), "evidence": collab.get("evidence"),
                          "failures": [f.get("detail") for f in collab.get("failures") or []]},
        "security": {"static_gate": static_status, "violations": gate.get("static") or {},
                     "sandbox": res.get("sandbox"), "credentials_in_sandbox": False,
                     "load_errors": "PASS" if gate.get("no_load_errors") else ("FAIL" if gate else "NOT TESTED")},
        "cards": {"status": cards_status, "emitted": cards_emitted, "problems": card_problems[:10]},
        "external_actions": receipts,
        "publication": publication,
        "spend": {k: cost.get(k) for k in ("total_usd", "cap_usd", "estimated_usd", "calculated_usd",
                                           "provider_reported_usd", "provider_reported_calls",
                                           "approximated_usage_calls")},
        "claims": claims,
        "limitations": limitations,
        "untested_assumptions": untested,
        "evidence": [{"path": _rel(p), "sha256": sha256_file(p)} for p in artifacts],
        "trace": _rel(jd / "trace.jsonl") if (jd / "trace.jsonl").exists() else None,
        "experiments": exps,
        "ledger_anchor": _ledger_anchor(job_id),
        "verdict": verdict,
    }
    body["integrity"] = {"body_sha256": sha256_bytes(canonical(body)),
                         "note": "sha256 over the canonical JSON of every other field; not a signature"}
    return body


def _contract_section(c: dict | None, jd: Path) -> dict:
    job = json.loads((jd / "job.json").read_text()) if (jd / "job.json").exists() else {}
    appr = job.get("contract_approval")
    return {"multi_agent": bool(c), "min_agents": (c or {}).get("min_agents", 1), "agents": (c or {}).get("agents"),
            "entry": (c or {}).get("entry"), "must_receive": (c or {}).get("must_receive"),
            "source": (c or {}).get("source"), "sha256": (c or {}).get("sha256"), "body": c,
            "approved_by_user": bool(appr) and appr.get("sha256") == (c or {}).get("sha256"),
            "approval": appr, "derived_by": "deterministic parser of the user's words (no model)"}


def _skills_digest(version: str | None) -> str | None:
    from . import skills as sk
    if not version:
        return None
    p = sk.VERSIONS / version
    if not p.exists():
        p = sk.CANDIDATES / version
    return _dir_digest(p) if p.exists() else None


def write(job_id: str, job_dir: Path | None = None) -> Path:
    jd = job_dir or (config.RUNS / job_id)
    pp = build_passport(job_id, jd)
    out = jd / "passport.json"
    out.write_text(json.dumps(pp, indent=1, default=str))
    ledger.append("passport_issued", job_id=job_id, passport_id=pp["passport_id"], verdict=pp["verdict"],
                  body_sha256=pp["integrity"]["body_sha256"])
    return out


def validate(path: Path, runs_dir: Path | None = None) -> list[str]:
    """Deterministic validation. Returns [] when the passport is internally consistent AND matches the files."""
    runs = runs_dir or config.RUNS
    problems: list[str] = []
    try:
        pp = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        return [f"unreadable passport: {e}"]
    for k in REQUIRED:
        if k not in pp:
            problems.append(f"missing field {k!r}")
    if problems:
        return problems
    if pp["schema"] != SCHEMA:
        problems.append(f"unknown schema {pp['schema']!r}")
    body = {k: v for k, v in pp.items() if k != "integrity"}
    if sha256_bytes(canonical(body)) != pp["integrity"].get("body_sha256"):
        problems.append("integrity: body digest does not match (the passport was edited after issue)")
    for c in pp["claims"]:
        if c.get("status") not in STATUSES:
            problems.append(f"claim {c.get('claim')!r} has invalid status {c.get('status')!r}")
        if c.get("kind") not in KINDS:
            problems.append(f"claim {c.get('claim')!r} has invalid kind {c.get('kind')!r}")
        if c.get("kind") == "INFERRED" and c.get("status") == "PASS":
            problems.append(f"INFERRED claim {c['claim']!r} cannot be PASS: inference is not verification")
        if c.get("kind") == "OBSERVED" and c.get("status") in ("PASS", "FAIL") and not c.get("evidence"):
            problems.append(f"OBSERVED claim {c['claim']!r} cites no evidence artifact")
    for section in ("tests", "collaboration", "cards"):
        if pp[section].get("status") not in STATUSES:
            problems.append(f"{section}.status {pp[section].get('status')!r} is not one of {STATUSES}")
    # files on disk must match what the passport says
    job_dir = runs / pp["build"]["job_id"]
    for e in pp["evidence"]:
        f = runs / e["path"]
        if not f.exists():
            problems.append(f"evidence file missing: {e['path']}")
        elif sha256_file(f) != e["sha256"]:
            problems.append(f"evidence file changed since issue: {e['path']}")
    if (job_dir / "final" / "agents").exists():
        cd = code_digest(job_dir)
        if cd["system_sha256"] != pp["code"]["system_sha256"]:
            problems.append("code digest does not match final/agents (the code changed after verification)")
    else:
        problems.append("final/agents not found: code digest cannot be re-derived")
    # ledger anchor
    ok, n = ledger.verify_chain()
    if not ok:
        problems.append(f"ledger hash chain is broken at record {n}")
    anc = pp["ledger_anchor"]
    if anc.get("hash"):
        recs = ledger.read()
        i = anc.get("index")
        if i is None or i >= len(recs) or recs[i]["hash"] != anc["hash"] or recs[i].get("job_id") != pp["build"]["job_id"]:
            problems.append("ledger anchor not found at the recorded position")
    else:
        problems.append("no ledger anchor: the build was never recorded in the evidence ledger")
    # verdict consistency: VERIFIED is only allowed when every blocking check passed
    if pp["verdict"] == "VERIFIED":
        if pp["tests"]["status"] != "PASS":
            problems.append("verdict VERIFIED but tests are not PASS")
        if pp["security"]["static_gate"] != "PASS":
            problems.append("verdict VERIFIED but the static gate is not PASS")
        if pp["cards"]["status"] == "FAIL":
            problems.append("verdict VERIFIED but card validation FAILED")
        if pp["collaboration"]["status"] not in ("PASS", "NOT TESTED") or (
                pp["collaboration"]["status"] == "NOT TESTED" and pp["code"]["agent_count"] > 1):
            problems.append("verdict VERIFIED but multi-agent collaboration was not observed")
        if pp["examiner"].get("certifies_itself"):
            problems.append("verdict VERIFIED by a builder that certified itself")
    con = pp["contract"]
    if con.get("multi_agent"):
        from . import contract as contract_mod
        body = con.get("body") or {}
        if con.get("sha256") != contract_mod.digest(body) or body.get("sha256") != con.get("sha256"):
            problems.append("contract digest does not match its body (the user contract was altered)")
        if pp["verdict"] == "VERIFIED":
            if pp["collaboration"]["status"] != "PASS":
                problems.append(f"verdict VERIFIED but the user contract requires {con.get('min_agents')} agents and "
                                f"collaboration is {pp['collaboration']['status']}")
            if pp["code"]["agent_count"] < (con.get("min_agents") or 0):
                problems.append(f"verdict VERIFIED with {pp['code']['agent_count']} agent(s); the user contract "
                                f"requires at least {con.get('min_agents')}")
    pub = pp["publication"]
    if pub["status"] == "DISCOVERABLE" and pub.get("discoverable") != "PASS":
        problems.append("publication DISCOVERABLE without a passing discoverability check")
    return problems


def summary_rows(pp: dict) -> list[tuple[str, str]]:
    """Rows for the ASI:One Proof Passport card (Card B)."""
    col = pp["collaboration"]
    ev = col.get("evidence") or {}
    rx = pp["external_actions"]
    return [
        ("Passport", pp["passport_id"]),
        ("Verdict", pp["verdict"]),
        ("Generated system", f"{pp['code']['agent_count']} agent(s), entry {pp['code']['entry']} · "
                             f"code sha256 {pp['code']['system_sha256'][:12]}…"),
        ("Independent tests", f"{pp['tests']['status']}: {pp['tests']['passed']}/{pp['tests']['executed']} "
                              f"(author {pp['examiner']['provider']}, never saw the code)"),
        ("Agent communication", f"{col['status']}" + (": " + ", ".join(
            f"{n} {v.get('in_passing_test')} matched send+receive in passing tests" for n, v in ev["peers"].items())
            if ev.get("peers") else "")),
        ("Security", f"static gate {pp['security']['static_gate']} · sandbox {pp['security']['sandbox']} · "
                     "no credentials in sandbox"),
        ("User contract", ("at least " + str(pp["contract"]["min_agents"]) + " agents"
                           + (f" ({', '.join(pp['contract']['agents'])})" if pp["contract"].get("agents") else "")
                           + (" · approved by you" if pp["contract"].get("approved_by_user") else "")
                           ) if pp["contract"]["multi_agent"] else "single agent (no multi-agent requirement)"),
        ("Cards", f"{pp['cards']['status']} ({pp['cards']['emitted']} emitted)"),
        ("External actions", "; ".join(f"{r['action'][:50]}: {r['status']}" for r in rx) or "none yet"),
        ("Publication", f"{pp['publication']['status']} · discoverable {pp['publication']['discoverable']}"),
        ("Spend", f"${(pp['spend'].get('total_usd') or 0):.4f} of ${pp['spend'].get('cap_usd') or 0} cap "
                  f"(provider-reported: {pp['spend'].get('provider_reported_usd')})"),
        ("Remaining uncertainty", "; ".join(pp["untested_assumptions"])[:300]),
        ("Validate", f"python -m foundry passport-verify runs/{pp['build']['job_id']}/passport.json"),
    ]
