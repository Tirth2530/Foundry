"""The System That Gets Better: FOUNDRY's self-improvement loop.

round:
  1. build every dev task with the CURRENT skills (full failure details are allowed here)
  2. Failure Miner clusters failures -> one root cause
  3. Skill Evolver proposes ONE patch to FOUNDRY itself (lesson / tool / routing / budget)
  4. Sealed Promotion Judge runs current vs candidate on the sealed promotion set
  5. promote (new version) or reject (candidate discarded = automatic rollback)
Every step is written to the hash-chained ledger.
"""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor

from . import bench, config, ledger, llm, pipeline, skills as sk
from .roles import evolver, judge, miner


def build_dev(skillset, tag: str, limit: int | None = None) -> list[dict]:
    specs = bench.load("dev", purpose="learning")
    if limit:  # deterministic subset (sorted by id) for cheaper rounds
        specs = specs[:limit]

    def one(spec):
        jid = f"dev_{skillset.version}_{spec['id']}{tag}"
        return pipeline.build(spec["request"], skillset, budget_usd=config.DEFAULT_BUDGET_USD, job_id=jid,
                              hidden_tests=spec["tests"], spec_id=spec["id"], out_dir=config.RUNS / "dev" / jid,
                              topology=spec.get("topology"))
    with ThreadPoolExecutor(max_workers=config.PARALLEL_BUILDS) as ex:
        return list(ex.map(one, specs))


def _cached_promotion(version: str):
    for r in reversed(ledger.read("promotion_eval")):
        if r.get("version") == version and r.get("_solved_set") is not None:
            return r
    return None


def gate(cur, cluster: dict, history: list, meter, repeats: int, say, origin: str) -> dict:
    """Propose ONE patch for `cluster`, test it on the sealed promotion set, promote or roll back."""
    patch = evolver.propose(cluster, cur, meter, history)
    cand_id = f"c{len(history) + 1:04d}"
    cand = sk.make_candidate(cur, patch, cand_id)
    say(f"evolver: {patch['kind']} {patch.get('file', patch.get('config_changes', ''))} :: {patch.get('rationale', '')[:160]}")
    ledger.append("patch_proposed", candidate=cand_id, parent=cur.version, patch=patch, origin=origin,
                  cluster={k: cluster[k] for k in ("code", "count", "spec_count", "root_cause") if k in cluster})
    cur_eval = _cached_promotion(cur.version)
    if cur_eval is None:
        cur_eval = judge.evaluate(cur, purpose=f"promotion baseline for {cur.version}", repeats=repeats)
        ledger.append("promotion_eval", **cur_eval)
    cand_eval = judge.evaluate(cand, purpose=f"promotion candidate {cand_id}", repeats=repeats)
    ledger.append("promotion_eval", **cand_eval)
    ok, reason = judge.decide(cur_eval, cand_eval)
    decision = {"candidate": cand_id, "parent": cur.version, "patch": patch, "reason": reason, "origin": origin,
                "root_cause": cluster.get("root_cause"), "current": judge.public(cur_eval),
                "candidate_eval": judge.public(cand_eval), "diff": sk.diff_summary(cur, cand)}
    if ok:
        new_v = sk.promote(cand)
        ledger.append("promotion_eval", **{**cand_eval, "version": new_v})
        decision.update(decision="promoted", new_version=new_v)
        say(f"PROMOTED -> {new_v}: {reason}")
    else:
        decision.update(decision="rejected")
        say(f"REJECTED (rolled back to {cur.version}): {reason}")
    ledger.append("patch_decision", **decision)
    history.append(decision)
    return decision


def spent_since(t_start: str) -> float:
    return round(sum(b.get("usd") or 0 for b in ledger.read("build") if b["t"] >= t_start), 4)


def estimate_round(dev_limit: int | None, repeats: int, usd_per_build: float | None = None) -> dict:
    n_dev = min(dev_limit or 99, len(bench._load_dir(bench.DIRS["dev"])))
    n_prom = len(bench._load_dir(bench.DIRS["promotion"])) * repeats
    hist = [b["usd"] for b in ledger.read("build") if b.get("usd")][-30:]
    per = usd_per_build or (sum(hist) / len(hist) if hist else None)
    first = _cached_promotion(sk.current_version()) is None
    builds = n_dev + n_prom * (2 if first else 1)
    return {"builds": builds, "usd_per_build": per, "usd_estimate": round(builds * per, 2) if per else None,
            "note": "includes a one-time promotion baseline" if first else "promotion baseline cached"}


def preflight(say=print) -> None:
    """Fail in seconds, before spending anything, if the environment can't produce valid evidence."""
    from .harness import run as harness
    problems = []
    try:
        say(f"preflight: sandbox mode = {harness.sandbox_mode()}")
    except harness.InfrastructureError as e:
        problems.append(str(e))
    for role, prov in (("builder", "gpt"), ("examiner", "meta")):
        if not llm.available(sk.load().route(role)):
            say(f"preflight: note - {role} model '{sk.load().route(role)}' has no key; a fallback will be used")
    if not any(llm.available(p) for p in config.PROVIDERS):
        problems.append("no model API keys configured")
    if problems:
        raise SystemExit("PREFLIGHT FAILED (nothing was spent):\n  - " + "\n  - ".join(problems))


def run(rounds: int, repeats: int = 1, say=print, dev_limit: int | None = None, max_usd: float | None = None) -> None:
    preflight(say)
    history = ledger.read("patch_decision")
    meter = llm.CostMeter()
    t_start = time.strftime("%Y-%m-%dT%H:%M:%S")
    for rnd in range(1, rounds + 1):
        est = estimate_round(dev_limit, repeats)
        say(f"round estimate: {est}")
        if max_usd is not None and est["usd_estimate"] and spent_since(t_start) + est["usd_estimate"] > max_usd:
            say(f"STOP: next round (~${est['usd_estimate']}) would exceed --max-usd {max_usd} "
                f"(spent ${spent_since(t_start)} so far)")
            break
        cur = sk.load()
        say(f"\n=== round {rnd}: current skills {cur.version} ({cur.prompt_tokens()} prompt tokens) ===")
        dev = build_dev(cur, tag=f"_r{len(history)}", limit=dev_limit)
        solved = sum(1 for b in dev if (b.get("hidden") or {}).get("solved"))
        say(f"dev: {solved}/{len(dev)} tasks solved, ${sum(b['cost']['total_usd'] for b in dev):.3f}")
        ledger.append("dev_round", version=cur.version, solved=solved, tasks=len(dev),
                      usd=round(sum(b["cost"]["total_usd"] for b in dev), 4))
        infra = [b for b in dev if b.get("error_kind") == "infrastructure"]
        if infra:
            say(f"ABORTING ROUND: {len(infra)}/{len(dev)} builds hit an environment problem, not a skill problem. "
                f"Nothing will be learned from it.\n  first error: {infra[0].get('error', '')[:300]}")
            ledger.append("round_aborted", reason="infrastructure", error=infra[0].get("error", "")[:300])
            break
        cluster = miner.mine(dev, cur, meter)
        if not cluster:
            say("no failures on dev: nothing to learn this round")
            break
        say(f"miner: top cluster {cluster['code']} x{cluster['count']} over {cluster['spec_count']} tasks")
        say(f"  root cause: {cluster.get('root_cause', '')[:240]}")
        from .harness.run import InfrastructureError
        try:
            gate(cur, cluster, history, meter, repeats, say, origin=f"dev round on {cur.version}")
        except InfrastructureError as e:
            say(f"ABORTING ROUND during promotion: {e}. The candidate is discarded; nothing was promoted.")
            ledger.append("round_aborted", reason="infrastructure", error=str(e)[:300])
            break
    say(f"\nbuild spend this run: ${spent_since(t_start)} · meta-cost (miner+evolver): ${meter.total_usd:.3f}")


def job_failures(job_id: str) -> list[dict]:
    """Every defect the independent verifier caught during a build, INCLUDING ones the repair
    loop later fixed. Fixing the app is not enough; these are what FOUNDRY learns from."""
    from .dsl import failures
    out = []
    for vf in sorted((config.RUNS / job_id / "attempts").glob("*/verify.json")):
        data = json.loads(vf.read_text())
        for f in failures(data.get("results", [])):
            out.append({**f, "attempt": vf.parent.name})
        for name, tb in ((data.get("raw") or {}).get("load_errors") or {}).items():
            out.append({"code": "LOAD_ERROR", "case": name, "send": "", "detail": tb[-400:], "attempt": vf.parent.name})
    for sf in sorted((config.RUNS / job_id / "attempts").glob("*/feedback.txt")):
        for line in sf.read_text().splitlines():
            if line.startswith("[static:") or line.startswith("[card-audit:"):
                code = "STATIC_VIOLATION" if line.startswith("[static:") else "CARD_INVALID"
                out.append({"code": code, "case": "-", "send": "", "detail": line[:400], "attempt": sf.parent.name})
    return out


def learn_from_job(job_id: str, repeats: int = 1, say=print) -> dict | None:
    """Live self-improvement from ONE build: the defects caught while building job_id become one
    patch to FOUNDRY itself, which must beat the sealed promotion set (tasks it has never seen
    while learning) with zero regressions, or it is rolled back."""
    preflight(say)
    fails = job_failures(job_id)
    if not fails:
        say(f"{job_id}: the verifier caught no defects during this build, so there is nothing to learn from it")
        return None
    cur = sk.load()
    meter = llm.CostMeter()
    pseudo = [{"job_id": job_id, "spec_id": job_id, "status": "verified", "hidden": {"failures": fails}}]
    cluster = miner.mine(pseudo, cur, meter)
    say(f"miner: {cluster['code']} x{cluster['count']} caught in {job_id}; root cause: {cluster.get('root_cause', '')[:200]}")
    history = ledger.read("patch_decision")
    d = gate(cur, cluster, history, meter, repeats, say, origin=f"live build {job_id}")
    d["meta_usd"] = meter.total_usd
    return d


def _lessons_only_arm(base, cur):
    """Ablation: baseline routing, tools and repair budget plus ONLY the learned lessons. This
    is the 'same models, same budget, with vs without evolved skills' comparison."""
    import shutil
    dest = sk.CANDIDATES / f"ablation_{cur.version}"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(cur.path, dest)
    (dest / "config.json").write_text((base.path / "config.json").read_text())
    return sk.load(f"{cur.version}-lessons-only", dest)


FINAL_LOCK = "final.lock"


def evaluate_final(repeats: int = 2, say=print, reopen_reason: str | None = None) -> dict:
    """Run ONCE, at the end, on the untouched final set. Same budget cap for every arm.
      baseline       v0000: what an ordinary coding agent gets
      evolved        current version as promoted
      lessons-only   baseline models/tools/budget + learned lessons (controls for routing/budget)"""
    lock = config.EVIDENCE / FINAL_LOCK
    if lock.exists():
        if not reopen_reason:
            raise PermissionError(f"the final set was already used ({lock.read_text().strip()}). Results from a "
                                  "second look are no longer held-out; pass --reopen \"<reason>\" to do it anyway, "
                                  "which is recorded in the ledger and the report")
        ledger.append("final_reopened", reason=reopen_reason, previous=lock.read_text().strip())
    base = sk.load(sk.BASELINE)
    cur = sk.load()
    arms = [base] if cur.version == base.version else [base, cur]
    if cur.version != base.version and cur.config != base.config:
        arms.append(_lessons_only_arm(base, cur))
    out = {}
    bench._FINAL_UNLOCKED = True
    try:
        out = _run_final_arms(arms, repeats, say)
    finally:
        bench._FINAL_UNLOCKED = False
    config.EVIDENCE.mkdir(parents=True, exist_ok=True)
    lock.write_text(f"used {time.strftime('%Y-%m-%d %H:%M:%S')} hash {bench.split_hash('final')}\n")
    ledger.append("final_eval", results=out, final_hash=bench.split_hash("final"), reopened=bool(reopen_reason))
    (config.EVIDENCE / "final_eval.json").write_text(json.dumps(out, indent=1))
    return out


def _run_final_arms(arms, repeats, say) -> dict:
    out = {}
    for s in arms:
        say(f"final eval: {s.version} x{repeats} (budget cap ${config.DEFAULT_BUDGET_USD}/build) ...")
        agg = judge.evaluate(s, split="final", purpose=f"final evaluation {s.version}", repeats=repeats, tag="_final")
        out[s.version] = {**judge.public(agg), "routing": s.config.get("routing"),
                          "repair_budget": s.config.get("repair_budget"), "lessons": len(s.lessons)}
        say(json.dumps(out[s.version]))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--dev-limit", type=int, default=None)
    ap.add_argument("--max-usd", type=float, default=None)
    a = ap.parse_args()
    run(a.rounds, a.repeats, dev_limit=a.dev_limit, max_usd=a.max_usd)
