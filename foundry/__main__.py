"""FOUNDRY command line.  python -m foundry <command>

  doctor                     check keys, packages and every model provider with a 1-token call
  gate1                      Gate 1: create + deploy + independently invoke a new hosted agent
  selftest                   Gate 2 (no LLM needed): harness catches a real defect in a known agent
  build "<request>"          one verified build with the current skills  [--publish]
  serve [--direct]           run the agent network (Foreman on ASI:One + 14 role agents)
  evolve --rounds N          Gate 3 (--dev-limit N, --max-usd X, --estimate): self-improvement rounds (dev -> patch -> sealed promotion gate)
  eval-final [--repeats 2]   baseline v0000 vs current on the untouched final set (run once, at the end)
  sandbox-build              build the Docker sandbox (no network, no keys, resource-limited) for generated code
  learn job_xxxxxx           live self-improvement from ONE build's caught defects (sealed gate decides)
  history                    every patch FOUNDRY proposed, and what the gate decided
  rollback vNNNN             point CURRENT back to an earlier skill version
  claim job_xxxxxx           deploy a verified build into YOUR Agentverse account (CLAIM_AGENTVERSE_API_KEY)
  passport job_xxxxxx        (re)issue the Proof Passport for a build -> runs/<job>/passport.json
  passport-verify <file>     deterministic check of a passport against the files and the ledger (exit 1 if invalid)
  recheck job_xxxxxx         re-test whether a deployed agent is in the Agentverse search index
  recover                    tear down any deployment left half-finished by a crash (confirmed by a 404)
  report                     write evidence/report.md (numbers for the pitch)
  lint                       validate specs, manifest, cross-split leakage and the ledger hash chain (exit 1 on error)
  bench-freeze               write bench/MANIFEST.json (sha256 + fingerprint per spec; final set locked once used)
  search "<what you need>"   RAW Agentverse search only: is the search API returning real agents? (no LLM)
  scout "<what you need>"    full Scout: search Agentverse, then live-test the top matches (reuse vs build)
"""
from __future__ import annotations

import argparse
import json
import sys


def cmd_doctor(_):
    from . import config, llm
    ok = True
    for mod in ("uagents", "uagents_core", "requests"):
        try:
            __import__(mod)
            print(f"[ok] package {mod}")
        except ImportError:
            ok = False
            print(f"[!!] package {mod} missing: pip install -r requirements.txt")
    for p, c in config.PROVIDERS.items():
        if not c["api_key"]:
            print(f"[--] {p}: no key (will fall back to another provider)")
            continue
        try:
            r = llm.chat([{"role": "user", "content": "Reply with the single word OK."}], provider=p, role="doctor",
                         max_tokens=20)
            print(f"[ok] {p} ({c['model']}): {r['text'].strip()[:20]!r} {r['tokens_in']}+{r['tokens_out']} tokens")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"[!!] {p} ({c['model']}): {e}")
    if config.AGENTVERSE_API_KEY:
        from . import agentverse
        pr = agentverse.Client().probe_api()  # SAME version resolution as deployment (AGENTVERSE_HOSTING_API)
        ok &= pr["ok"]
        print(f"[{'ok' if pr['ok'] else '!!'}] Agentverse hosting API {pr['api']} (tried: {'; '.join(pr['tried'])})")
    else:
        print("[--] AGENTVERSE_API_KEY not set (needed for gate1/publish)")
    from .agents import payments
    print(f"[{'ok' if payments.enabled() else '--'}] Stripe test payments {'enabled' if payments.enabled() else 'off (demo mode)'}")
    from .harness import run as harness
    try:
        print(f"[ok] sandbox mode: {harness.sandbox_mode()}")
    except harness.SandboxNotConfigured as e:
        ok = False
        print(f"[!!] sandbox: {e}")
    sys.exit(0 if ok else 1)


def cmd_gate1(_):
    import runpy
    from . import config
    runpy.run_path(str(config.ROOT / "tools" / "gate1_spike.py"), run_name="__main__")


def cmd_selftest(_):
    from . import config
    from .harness import run as harness
    from .roles import card_validator
    ref = config.ROOT / "tools" / "reference"
    spec = json.loads((ref / "spec.json").read_text())
    good = (ref / "good_agent.py").read_text()
    bad = (ref / "bad_agent.py").read_text()
    print("running the CORRECT reference agent against its hidden tests...")
    g = harness.run([{"name": "main", "code": good}], "main", spec["tests"])
    gs = harness.summarize(g["results"])
    print(f"  {gs['passed']}/{gs['cases']} passed; card audit issues: {len(card_validator.audit(g['raw']))}")
    print("running a DEFECTIVE variant (wrong card key, no JSON-selection handling)...")
    b = harness.run([{"name": "main", "code": bad}], "main", spec["tests"])
    bs = harness.summarize(b["results"])
    from .dsl import failures
    fl = failures(b["results"])
    print(f"  {bs['passed']}/{bs['cases']} passed; caught {len(fl)} failing checks:")
    for f in fl[:5]:
        print(f"   - {f['code']}: {f['detail'][:140]}")
    print("running a REAL two-agent system and checking the observed hand-off...")
    from . import topology
    multi = [{"name": "upper", "code": (ref / "multi_upper.py").read_text()},
             {"name": "coordinator", "code": (ref / "multi_coordinator.py").read_text()}]
    m = harness.run(multi, "coordinator", [{"name": "m", "turns": [{"send": "hello agents", "checks": [
        {"type": "text_contains", "value": "UPPER: HELLO AGENTS"}]}]}])
    tfail = topology.check_spec({"agents": ["upper", "coordinator"], "must_receive": ["upper"]}, m["raw"],
                                ["upper", "coordinator"], "coordinator", m["results"])
    multi_ok = m["results"][0]["passed"] and m["raw"].get("traffic_tap") and m["raw"].get("receive_tap") and not tfail
    print(f"  reply ok: {m['results'][0]['passed']}; traffic observed: {m['raw'].get('traffic')}; "
          f"tap installed: {m['raw'].get('traffic_tap')}")
    print(f"  sandbox mode: {m.get('sandbox')}")
    passed = gs["passed"] == gs["cases"] and bs["passed"] < bs["cases"] and bool(multi_ok)
    (config.EVIDENCE).mkdir(exist_ok=True)
    (config.EVIDENCE / "gate2_selftest.json").write_text(json.dumps(
        {"good": gs, "bad": bs, "caught": fl, "multi_agent": {"ok": bool(multi_ok), "traffic": m["raw"].get("traffic"),
                                                              "topology_failures": tfail},
         "sandbox": m.get("sandbox"), "gate2": "PASS" if passed else "FAIL"}, indent=1))
    print(f"\nGATE 2: {'PASS' if passed else 'FAIL'} (evidence/gate2_selftest.json)")
    if not passed and g["stderr_tail"]:
        print("runner stderr:\n" + g["stderr_tail"])
    sys.exit(0 if passed else 1)


def cmd_build(a):
    from . import pipeline, skills as sk
    s = sk.load()

    def say(stage, info=None):
        print(f"  [{stage}] {json.dumps(info) if info else ''}"[:300])
    res = pipeline.build(a.request, s, budget_usd=a.budget, progress=say)
    print(json.dumps({k: res.get(k) for k in ("job_id", "status", "verify", "examiner", "cost", "seconds", "error")},
                     indent=1, default=str))
    if a.publish and res["status"] == "verified":
        from .roles import livecheck, publisher
        from . import config
        exam = json.loads((config.RUNS / res["job_id"] / "examiner_tests.json").read_text())
        print(json.dumps(publisher.publish(res, livecheck.pick_case(exam.get("cases"))), indent=1))


def cmd_serve(a):
    import asyncio
    from uagents import Bureau
    from . import config
    from .agents import foreman as fm
    agent = fm.create_foreman_agent()
    if a.direct or config.NETWORK_MODE == "direct":
        fm.Foreman(agent)
        print("FOUNDRY Foreman (direct mode: roles run in-process)")
        agent.run()
        return
    from .agents import network
    roles = network.build_roles(agent.address)  # role agents accept tasks from the Foreman only
    network.install(agent, roles)
    fm.Foreman(agent)
    print(f"FOUNDRY agent network: Foreman {agent.address} + {len(roles)} role agents")
    for name, r in roles.items():
        print(f"  {name:15s} {r.address}")
    if config.NETWORK_MODE == "bureau":
        bureau = Bureau(port=config.FOREMAN_PORT)
        bureau.add(agent)
        for r in roles.values():
            bureau.add(r)
        bureau.run()
        return
    # default "split": the Foreman runs as a normal mailbox agent (reachable from ASI:One) and the
    # role agents run in a Bureau on the same event loop; uAgents delivers between them locally.
    roles_bureau = Bureau(port=config.FOREMAN_PORT + 1)
    for r in roles.values():
        roles_bureau.add(r)
    loop = asyncio.get_event_loop()
    loop.run_until_complete(asyncio.gather(agent.run_async(), roles_bureau.run_async()))


def cmd_evolve(a):
    from . import evolve
    if a.estimate:
        print(json.dumps(evolve.estimate_round(a.dev_limit, a.repeats), indent=1))
        return
    evolve.run(a.rounds, a.repeats, dev_limit=a.dev_limit, max_usd=a.max_usd)


def cmd_eval_final(a):
    from . import evolve
    print(json.dumps(evolve.evaluate_final(a.repeats, reopen_reason=a.reopen), indent=1))


def cmd_sandbox_build(_):
    import subprocess
    from . import config
    from .harness import run as h
    r = subprocess.run(["docker", "build", "-t", h.IMAGE, str(config.ROOT / "sandbox")])
    print(f"sandbox image {h.IMAGE}: {'ready' if r.returncode == 0 else 'FAILED'} (mode now: {h.sandbox_mode()})")


def cmd_learn(a):
    from . import evolve
    print(json.dumps(evolve.learn_from_job(a.job), indent=1, default=str))


def cmd_history(_):
    from . import ledger, skills as sk
    print(f"current: {sk.current_version()}  versions: {sk.list_versions()}")
    for d in ledger.read("patch_decision"):
        p = d["patch"]
        print(f"{d['candidate']} ({d['parent']}) {d['decision'].upper():9s} {p['kind']} {p.get('file', p.get('config_changes', ''))}"
              f"\n    {d['current'].get('solved')}/{d['current'].get('tasks')} -> {d['candidate_eval'].get('solved')}/"
              f"{d['candidate_eval'].get('tasks')}  ${d['candidate_eval'].get('usd_per_build')}/build :: {d['reason']}")


def cmd_rollback(a):
    from . import ledger, skills as sk
    sk.rollback(a.version)
    ledger.append("rollback", to=a.version, by="cli")
    print(f"CURRENT -> {a.version}")


def cmd_claim(a):
    import os
    from . import agentverse, config
    from .roles import livecheck, publisher
    key = os.getenv("CLAIM_AGENTVERSE_API_KEY")
    if not key:
        sys.exit("Set CLAIM_AGENTVERSE_API_KEY to the Agentverse API key of the account that should own the agent.")
    res = json.loads((config.RUNS / a.job / "result.json").read_text())
    if res["status"] != "verified":
        sys.exit("only verified builds can be claimed")
    exam = json.loads((config.RUNS / a.job / "examiner_tests.json").read_text())
    out = publisher.publish(res, livecheck.pick_case(exam.get("cases")), client=agentverse.Client(api_key=key))
    print(json.dumps(out, indent=1))


def cmd_search(a):
    from . import agentverse
    c = agentverse.Client()
    for semantic in (True, False):
        try:
            hits = c.search_agents(a.text, limit=10, semantic=semantic)
        except agentverse.AgentverseError as e:
            print(f"[{'semantic' if semantic else 'keyword'}] SEARCH FAILED: {e}")
            continue
        print(f"\n[{'semantic' if semantic else 'keyword'} search] {len(hits)} agents for {a.text!r}")
        for h in hits:
            chat = "chat" if agentverse.has_chat_protocol(h) else "no-chat"
            print(f"  {h.get('status', '?'):7s} {chat:7s} {str(h.get('name'))[:40]:40s} {h.get('address')}")
            readme = str(h.get("readme") or h.get("description") or "").replace("\n", " ")
            print(f"           readme: {readme[:110]!r}  success={h.get('recent_success_rate')}")


def cmd_scout(a):
    from . import llm, skills as sk
    from .roles import scout
    res = scout.find(a.text, sk.load(), llm.CostMeter(cap_usd=0.5), [])
    print(f"search: {res['search_note']}  queries={res['search'].get('queries')}")
    for e in res["search"].get("errors", []):
        print(f"  search error: {e}")
    print(f"considered: {res['considered']}")
    for au in res["audits"]:
        c, need, claim = au["candidate"], au.get("need") or {}, au.get("claim")
        print(f"\n  {c.get('name')} {c['address']} [{c['source']}] listing={c.get('platform')}")
        print(f"    does what you need : {need.get('passed')}  reply={(need.get('texts') or ['(none)'])[0][:120]!r}")
        print(f"    does what it claims: {None if claim is None else claim.get('passed')}  claim={au.get('claim_text')!r}")
    print(f"\nDECISION: {'REUSE ' + res['candidate']['address'] if res['found'] else 'BUILD NEW (nothing passed live)'}")


def cmd_recheck(a):
    from .roles import publisher
    out = publisher.recheck(a.job, wait_s=a.wait)
    print(json.dumps({k: out.get(k) for k in ("publication_status", "entry", "checks", "discovery")}, indent=1))
    sys.exit(0 if out["publication_status"] == "DISCOVERABLE" else 1)


def cmd_passport(a):
    from . import passport
    path = passport.write(a.job)
    pp = json.loads(path.read_text())
    for k, v in passport.summary_rows(pp):
        print(f"  {k:22s} {v}")
    print(f"\nwritten: {path}")


def cmd_passport_verify(a):
    from pathlib import Path
    from . import passport
    problems = passport.validate(Path(a.path))
    if problems:
        print("PASSPORT INVALID:")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    pp = json.loads(Path(a.path).read_text())
    print(f"PASSPORT VALID: {pp['passport_id']} verdict={pp['verdict']} code={pp['code']['system_sha256'][:16]} "
          f"evidence files={len(pp['evidence'])} ledger anchor={pp['ledger_anchor']['hash']}")


def cmd_recover(_):
    from . import jobs
    out = jobs.recover_all_publishes()
    print(json.dumps(out, indent=1, default=str))
    sys.exit(1 if any(o.get("cleanup_confirmed") is False for o in out) else 0)


def cmd_report(_):
    from . import report
    print(report.write())


def cmd_lint(_):
    from . import bench, ledger
    rep = bench.lint_all()
    bad = {k: v for k, v in rep.items() if v}
    print(f"{len(rep)} specs, {len(bad)} with problems")
    for k, v in bad.items():
        print(f"  {k}: {v}")
    ok, n = ledger.verify_chain()
    print(f"ledger chain {'intact' if ok else 'BROKEN at record ' + str(n)} ({n} records)")
    for split in bench.DIRS:
        print(f"  {split} content hash {bench.split_hash(split)}")
    integ = bench.integrity()
    print(f"integrity: {integ['counts']} (multi-agent {integ['multi_agent']}), "
          f"promotion set evaluated {integ['promotion_set_evaluations']}x")
    for w in integ["warnings"]:
        print(f"  warning: {w}")
    for e in integ["errors"]:
        print(f"  ERROR: {e}")
    sys.exit(1 if bad or integ["errors"] or not ok else 0)


def cmd_bench_freeze(a):
    from . import bench
    m = bench.freeze(force_final=a.force_final)
    print(f"froze {len(m['files'])} spec files -> bench/MANIFEST.json; split hashes {m['split_hashes']}")


def main():
    ap = argparse.ArgumentParser(prog="foundry", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    sub.add_parser("gate1")
    sub.add_parser("selftest")
    b = sub.add_parser("build")
    b.add_argument("request")
    b.add_argument("--budget", type=float, default=None)
    b.add_argument("--publish", action="store_true")
    s = sub.add_parser("serve")
    s.add_argument("--direct", action="store_true")
    e = sub.add_parser("evolve")
    e.add_argument("--rounds", type=int, default=3)
    e.add_argument("--repeats", type=int, default=1)
    e.add_argument("--dev-limit", type=int, default=None, help="use only the first N dev tasks (cheaper rounds)")
    e.add_argument("--max-usd", type=float, default=None, help="stop before a round that would exceed this spend")
    e.add_argument("--estimate", action="store_true", help="print the cost estimate for one round and exit")
    f = sub.add_parser("eval-final")
    f.add_argument("--repeats", type=int, default=2)
    f.add_argument("--reopen", default=None, help="reason for reusing an already-used final set (logged)")
    sub.add_parser("history")
    sub.add_parser("sandbox-build")
    le = sub.add_parser("learn")
    le.add_argument("job")
    r = sub.add_parser("rollback")
    r.add_argument("version")
    c = sub.add_parser("claim")
    c.add_argument("job")
    pa = sub.add_parser("passport")
    pa.add_argument("job")
    pv = sub.add_parser("passport-verify")
    pv.add_argument("path")
    rc = sub.add_parser("recheck")
    rc.add_argument("job")
    rc.add_argument("--wait", type=float, default=0, help="keep polling the search index for N seconds")
    sub.add_parser("recover")
    sub.add_parser("report")
    sub.add_parser("lint")
    bf = sub.add_parser("bench-freeze")
    bf.add_argument("--force-final", action="store_true")
    se = sub.add_parser("search")
    se.add_argument("text")
    sc = sub.add_parser("scout")
    sc.add_argument("text")
    a = ap.parse_args()
    {"doctor": cmd_doctor, "gate1": cmd_gate1, "selftest": cmd_selftest, "build": cmd_build, "serve": cmd_serve,
     "evolve": cmd_evolve, "eval-final": cmd_eval_final, "history": cmd_history, "rollback": cmd_rollback,
     "claim": cmd_claim, "learn": cmd_learn, "sandbox-build": cmd_sandbox_build, "report": cmd_report, "recheck": cmd_recheck,
     "passport": cmd_passport, "recover": cmd_recover, "passport-verify": cmd_passport_verify, "lint": cmd_lint, "bench-freeze": cmd_bench_freeze, "search": cmd_search,
     "scout": cmd_scout}[a.cmd](a)


if __name__ == "__main__":
    main()
