"""Sealed Promotion Judge. Runs a skill version on the sealed promotion set and returns
ONLY aggregates to the rest of the system. Detailed results are written to a sealed log.

Promotion rule (strict, two-sided):
  accept  iff  candidate solves strictly more promotion tasks than the current version
               AND no task the current version solved is lost (zero regressions)
  also accept a pure cost win: same solved set, >= 15% cheaper per build
  otherwise reject (and the candidate is discarded: an automatic rollback)
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from .. import bench, config, pipeline

SEALED_LOG = config.EVIDENCE / "sealed"


def evaluate(skillset, split: str = "promotion", *, purpose: str, repeats: int = 1, tag: str = "") -> dict:
    specs = bench.load(split, purpose=purpose)
    jobs = [(s, r) for s in specs for r in range(repeats)]

    def one(job):
        spec, r = job
        jid = f"{split[:4]}_{skillset.version}_{spec['id']}_{r}{tag}"
        return pipeline.build(spec["request"], skillset, budget_usd=config.DEFAULT_BUDGET_USD, job_id=jid,
                              hidden_tests=spec["tests"], spec_id=spec["id"], topology=spec.get("topology"),
                              out_dir=config.RUNS / "sealed" / jid)

    with ThreadPoolExecutor(max_workers=config.PARALLEL_BUILDS) as ex:
        builds = list(ex.map(one, jobs))
    per_spec: dict = {}
    for b in builds:
        h = b.get("hidden") or {"solved": False, "passed": 0, "cases": 0}
        s = per_spec.setdefault(b["spec_id"], {"solved_runs": 0, "runs": 0, "cases_passed": 0, "cases": 0})
        s["runs"] += 1
        s["solved_runs"] += int(bool(h.get("solved")))
        s["cases_passed"] += h.get("passed", 0)
        s["cases"] += h.get("cases", 0)
    solved = {k for k, v in per_spec.items() if v["solved_runs"] * 2 > v["runs"]}  # majority of repeats
    usd = sum(b["cost"]["total_usd"] for b in builds)
    agg = {"version": skillset.version, "split": split, "tasks": len(specs), "repeats": repeats,
           "solved": len(solved), "case_pass": sum(v["cases_passed"] for v in per_spec.values()),
           "case_total": sum(v["cases"] for v in per_spec.values()),
           "usd_total": round(usd, 4), "usd_per_build": round(usd / max(1, len(builds)), 4)}
    SEALED_LOG.mkdir(parents=True, exist_ok=True)
    (SEALED_LOG / f"{split}_{skillset.version}{tag}.json").write_text(
        json.dumps({"aggregate": agg, "per_spec": per_spec, "solved": sorted(solved)}, indent=1))
    agg["_solved_set"] = sorted(solved)  # stays inside the judge process; stripped before sharing
    return agg


def decide(cur: dict, cand: dict) -> tuple[bool, str]:
    cur_s, cand_s = set(cur["_solved_set"]), set(cand["_solved_set"])
    lost = cur_s - cand_s
    if lost:
        return False, f"regression: lost {len(lost)} task(s) the current version solved"
    if len(cand_s) > len(cur_s):
        return True, f"solves {len(cand_s)}/{cand['tasks']} vs {len(cur_s)}/{cur['tasks']}, zero regressions"
    if cand_s == cur_s and cur["usd_per_build"] > 0 and cand["usd_per_build"] <= 0.85 * cur["usd_per_build"]:
        return True, f"same tasks solved, {100 * (1 - cand['usd_per_build'] / cur['usd_per_build']):.0f}% cheaper per build"
    if cand["case_pass"] > cur["case_pass"] and len(cand_s) == len(cur_s):
        return False, "more cases pass but no additional task fully solved (not enough evidence)"
    return False, f"no improvement ({len(cand_s)} vs {len(cur_s)} tasks solved)"


def public(agg: dict) -> dict:
    return {k: v for k, v in agg.items() if not k.startswith("_")}
