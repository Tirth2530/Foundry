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
from concurrent.futures import ThreadPoolExecutor

from . import bench, config, ledger, llm, pipeline, skills as sk
from .roles import evolver, judge, miner


def build_dev(skillset, tag: str) -> list[dict]:
    specs = bench.load("dev", purpose="learning")

    def one(spec):
        jid = f"dev_{skillset.version}_{spec['id']}{tag}"
        return pipeline.build(spec["request"], skillset, budget_usd=config.DEFAULT_BUDGET_USD, job_id=jid,
                              hidden_tests=spec["tests"], spec_id=spec["id"], out_dir=config.RUNS / "dev" / jid)
    with ThreadPoolExecutor(max_workers=config.PARALLEL_BUILDS) as ex:
        return list(ex.map(one, specs))


def _cached_promotion(version: str):
    for r in reversed(ledger.read("promotion_eval")):
        if r.get("version") == version and r.get("_solved_set") is not None:
            return r
    return None


def run(rounds: int, repeats: int = 1, say=print) -> None:
    history = ledger.read("patch_decision")
    meter = llm.CostMeter()
    for rnd in range(1, rounds + 1):
        cur = sk.load()
        say(f"\n=== round {rnd}: current skills {cur.version} ({cur.prompt_tokens()} prompt tokens) ===")
        dev = build_dev(cur, tag=f"_r{len(history)}")
        solved = sum(1 for b in dev if (b.get("hidden") or {}).get("solved"))
        say(f"dev: {solved}/{len(dev)} tasks solved, ${sum(b['cost']['total_usd'] for b in dev):.3f}")
        ledger.append("dev_round", version=cur.version, solved=solved, tasks=len(dev),
                      usd=round(sum(b["cost"]["total_usd"] for b in dev), 4))

        cluster = miner.mine(dev, cur, meter)
        if not cluster:
            say("no failures on dev: nothing to learn this round")
            break
        say(f"miner: top cluster {cluster['code']} x{cluster['count']} over {cluster['spec_count']} tasks")
        say(f"  root cause: {cluster.get('root_cause', '')[:240]}")

        patch = evolver.propose(cluster, cur, meter, history)
        cand_id = f"c{len(history) + 1:04d}"
        cand = sk.make_candidate(cur, patch, cand_id)
        say(f"evolver: {patch['kind']} {patch.get('file', patch.get('config_changes', ''))} :: {patch.get('rationale', '')[:160]}")
        ledger.append("patch_proposed", candidate=cand_id, parent=cur.version, patch=patch,
                      cluster={k: cluster[k] for k in ("code", "count", "spec_count", "root_cause") if k in cluster})

        cur_eval = _cached_promotion(cur.version)
        if cur_eval is None:
            cur_eval = judge.evaluate(cur, purpose=f"promotion baseline for {cur.version}", repeats=repeats)
            ledger.append("promotion_eval", **cur_eval)
        cand_eval = judge.evaluate(cand, purpose=f"promotion candidate {cand_id}", repeats=repeats)
        ledger.append("promotion_eval", **cand_eval)
        ok, reason = judge.decide(cur_eval, cand_eval)
        decision = {"candidate": cand_id, "parent": cur.version, "patch": patch, "reason": reason,
                    "current": judge.public(cur_eval), "candidate_eval": judge.public(cand_eval),
                    "diff": sk.diff_summary(cur, cand)}
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
    say(f"\nmeta-cost of evolution (miner+evolver): ${meter.total_usd:.3f}")


def evaluate_final(repeats: int = 2, say=print) -> dict:
    """Baseline (v0000) vs current on the untouched final set. Run this ONCE, at the end."""
    base = sk.load(sk.BASELINE)
    cur = sk.load()
    out = {}
    for s in (base, cur) if cur.version != base.version else (base,):
        say(f"final eval: {s.version} x{repeats} ...")
        agg = judge.evaluate(s, split="final", purpose=f"final evaluation {s.version}", repeats=repeats, tag="_final")
        out[s.version] = judge.public(agg)
        say(json.dumps(out[s.version]))
    ledger.append("final_eval", results=out, final_hash=bench.split_hash("final"))
    (config.EVIDENCE / "final_eval.json").write_text(json.dumps(out, indent=1))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--repeats", type=int, default=1)
    a = ap.parse_args()
    run(a.rounds, a.repeats)
