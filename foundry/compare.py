"""Controlled comparison of builder versions on the SAME tasks, SAME acceptance tests, SAME per-build budget.

Experimental design adapted from Dapta's comparison runs (backend/dapta/comparisons.py, MIT, Copyright (c) 2026
Aditya Venkat): fixed task set and conditions, every arm measured identically, per-condition summaries, no
blended score. FOUNDRY's arms:
  A baseline      v0000: no evolved skills
  B lessons-only  baseline models, tools, routing and repair budget + ONLY the validated lessons
  C full evolved  the current promoted version (any routing/tool/budget changes included)
If C changed nothing but lessons, B and C are the same configuration and that is stated, not hidden.

Every metric is reported on its own, with its sample size. Nothing is combined into an invented score.
"""
from __future__ import annotations

import statistics

SMALL_SAMPLE = 30  # builds per arm below which the report says the comparison is too small for strong claims


def _rate(num, den):
    return None if not den else round(num / den, 3)


def per_build(b: dict) -> dict:
    """The only fields a comparison needs from one sealed build (no tests, no transcripts)."""
    h = b.get("hidden") or {}
    topo = h.get("topology")
    return {"spec_id": b.get("spec_id"), "status": b.get("status"), "solved": bool(h.get("solved")),
            "self_verified": b.get("status") == "verified", "usd": (b.get("cost") or {}).get("total_usd", 0.0),
            "seconds": b.get("seconds"), "multi_agent": bool(topo),
            "collab_ok": None if not topo else not topo.get("failures"),
            "infra_failure": b.get("status") in ("error", "budget_exceeded", "cancelled"),
            "repeat": b.get("repeat", 0)}


def metrics(builds: list[dict], baseline_solved: set | None = None) -> dict:
    n = len(builds)
    tasks = sorted({b["spec_id"] for b in builds})
    repeats = sorted({b["repeat"] for b in builds})
    by_rep = [[b for b in builds if b["repeat"] == r] for r in repeats]
    rep_rates = [_rate(sum(x["solved"] for x in rb), len(rb)) for rb in by_rep if rb]
    solved_tasks = {t for t in tasks if sum(b["solved"] for b in builds if b["spec_id"] == t) * 2 >
                    sum(1 for b in builds if b["spec_id"] == t)}
    ok = [b for b in builds if b["solved"]]
    secs = [b["seconds"] for b in builds if b["seconds"] is not None]
    usd = sum(b["usd"] or 0 for b in builds)
    verified = [b for b in builds if b["self_verified"]]
    multi = [b for b in builds if b["multi_agent"]]
    out = {
        "builds": n, "tasks": len(tasks), "repeats": len(repeats),
        "success_rate": _rate(len(ok), n),
        "success_rate_by_repeat": rep_rates,
        "success_rate_spread": None if len(rep_rates) < 2 else
        {"min": min(rep_rates), "max": max(rep_rates), "stdev": round(statistics.pstdev(rep_rates), 3)},
        "tasks_solved_majority": len(solved_tasks),
        "usd_total": round(usd, 4),
        "usd_per_successful_build": None if not ok else round(usd / len(ok), 4),
        "median_seconds": None if not secs else round(statistics.median(secs), 1),
        # the builder's own independent examiner said VERIFIED but the hidden acceptance tests failed
        "incorrect_verification_rate": _rate(sum(1 for b in verified if not b["solved"]), len(verified)),
        "inter_agent_success_rate": _rate(sum(1 for b in multi if b["collab_ok"]), len(multi)),
        "multi_agent_builds": len(multi),
        "infrastructure_failures": sum(b["infra_failure"] for b in builds),
        "end_to_end_deployment_success": "NOT TESTED (sealed comparisons do not deploy; see publish evidence)",
        "recovery_from_injected_failures": "NOT TESTED (no fault injection in this harness yet)",
        "_solved_tasks": sorted(solved_tasks),
    }
    if baseline_solved is not None:
        lost = baseline_solved - solved_tasks
        out["regressions_vs_baseline"] = sorted(lost)
        out["regression_rate"] = _rate(len(lost), len(baseline_solved))
        out["gained_vs_baseline"] = sorted(solved_tasks - baseline_solved)
    out["sample_note"] = ("small sample: treat differences as indicative, not as a general improvement claim"
                          if n < SMALL_SAMPLE else f"{n} builds per arm")
    return out


def public(m: dict) -> dict:
    return {k: v for k, v in m.items() if not k.startswith("_")}
