"""Pitch numbers, computed only from the ledger and evidence files (nothing hand-typed)."""
from __future__ import annotations

import json

from . import bench, config, ledger, skills as sk


def write() -> str:
    decisions = ledger.read("patch_decision")
    evals = ledger.read("promotion_eval")
    builds = ledger.read("build")
    final = ledger.read("final_eval")
    opened = ledger.read("final_set_opened")
    chain_ok, n = ledger.verify_chain()
    lines = ["# FOUNDRY evidence report", "",
             f"- Current skill version: **{sk.current_version()}** (baseline {sk.BASELINE}); versions: {', '.join(sk.list_versions())}",
             f"- Builds recorded: {len(builds)} · total model spend ${sum(b.get('usd') or 0 for b in builds):.2f}",
             f"- Patches proposed: {len(decisions)} · promoted {sum(d['decision'] == 'promoted' for d in decisions)}"
             f" · rejected and rolled back {sum(d['decision'] == 'rejected' for d in decisions)}",
             f"- Ledger: {n} hash-chained records, chain {'intact' if chain_ok else 'BROKEN'}",
             f"- Final set opened {len(opened)} time(s), reopened {len(ledger.read('final_reopened'))} time(s); "
             f"content hash {bench.split_hash('final')}", "",
             "## Evolution (sealed promotion set)", "",
             "| candidate | parent | patch | current solved | candidate solved | $/build | decision | reason |",
             "|---|---|---|---|---|---|---|---|"]
    curve = []
    for d in decisions:
        p, cu, ce = d["patch"], d["current"], d["candidate_eval"]
        lines.append(f"| {d['candidate']} | {d['parent']} | {p['kind']} {p.get('file', '')} | {cu.get('solved')}/{cu.get('tasks')} "
                     f"| {ce.get('solved')}/{ce.get('tasks')} | {ce.get('usd_per_build')} | {d['decision']} | {d['reason']} |")
        curve.append({"candidate": d["candidate"], "decision": d["decision"], "solved": ce.get("solved"),
                      "tasks": ce.get("tasks"), "usd_per_build": ce.get("usd_per_build"),
                      "current_solved": cu.get("solved")})
    by_v = {}
    for e in evals:
        by_v[e["version"]] = e
    lines += ["", "## Promotion-set score by version", "", "| version | solved | cases | $/build |", "|---|---|---|---|"]
    for v in sorted(k for k in by_v if k.startswith("v")):
        e = by_v[v]
        lines.append(f"| {v} | {e['solved']}/{e['tasks']} | {e['case_pass']}/{e['case_total']} | {e['usd_per_build']} |")
    if final:
        lines += ["", "## Final held-out evaluation (untouched until the end)", "", "```",
                  json.dumps(final[-1]["results"], indent=1), "```"]
    for g in ("gate1.json", "gate2_selftest.json"):
        p = config.EVIDENCE / g
        if p.exists():
            d = json.loads(p.read_text())
            lines.append(f"- {g}: {d.get('result') or d.get('gate2')}")
    out = config.EVIDENCE / "report.md"
    out.write_text("\n".join(lines) + "\n")
    (config.EVIDENCE / "curve.json").write_text(json.dumps(curve, indent=1))
    return str(out)
