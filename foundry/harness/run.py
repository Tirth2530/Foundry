"""Parent side of the Execution Runner: launch the isolated child, parse, judge."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

from .. import config, dsl


def run(agents: list[dict], entry: str, cases: list[dict], *, turn_timeout: float | None = None) -> dict:
    """Execute cases against the generated agents. Returns {raw, results, stderr_tail}."""
    turn_timeout = turn_timeout or config.TURN_TIMEOUT_S
    n_turns = max(1, sum(len(c["turns"]) for c in cases))
    hard = 25 + turn_timeout * n_turns
    job = {"agents": agents, "entry": entry, "turn_timeout": turn_timeout, "hard_limit": hard,
           "cases": [{"name": c.get("name"), "turns": [{"send": t["send"]} for t in c["turns"]]} for c in cases]}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(job, f)
        path = f.name
    env = {**os.environ, "PYTHONPATH": str(config.ROOT)}
    work = tempfile.mkdtemp(prefix="foundry_run_")  # agent storage files land here, not in the repo
    try:
        p = subprocess.run([sys.executable, "-m", "foundry.harness.runner", path], cwd=work,
                           capture_output=True, text=True, timeout=hard + 15, env=env)
        out, err = p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = (e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or "")) + "\n[parent] child killed: timeout"
    finally:
        os.unlink(path)
        shutil.rmtree(work, ignore_errors=True)
    raw = None
    for line in out.splitlines():
        if line.startswith("FOUNDRY_RESULT:"):
            raw = json.loads(line[len("FOUNDRY_RESULT:"):])
    stderr_tail = _interesting(err)
    if raw is None:
        raw = {"load_errors": {"__harness__": "runner produced no result"}, "cases": []}
    return {"raw": raw, "results": judge(cases, raw), "stderr_tail": stderr_tail}


def judge(cases: list[dict], raw: dict) -> list[dict]:
    load_errors = raw.get("load_errors") or {}
    results = []
    raw_cases = raw.get("cases") or []
    for i, case in enumerate(cases):
        if load_errors.get(raw.get("entry")) or "__harness__" in load_errors:
            results.append({"name": case.get("name"), "passed": False, "turns": [{
                "send": case["turns"][0]["send"], "ok": False, "reply_text": "", "cards": 0,
                "checks": [{"type": "load", "ok": False, "code": dsl.LOAD,
                            "detail": next(iter(load_errors.values()))[-600:]}]}]})
            continue
        rec = (raw_cases[i] if i < len(raw_cases) else None) or {"turns": [{"error": "case not executed"}]}
        results.append(dsl.evaluate_case(case, rec))
    return results


def _interesting(err: str, limit: int = 2500) -> str:
    keep = [l for l in err.splitlines()
            if any(k in l for k in ("Error", "Traceback", "Exception", "File \"<agent", "raise", "WARNING", "ERROR"))
            and "Almanac" not in l and "registration" not in l.lower() and "endpoint" not in l.lower()]
    return "\n".join(keep)[-limit:]


def summarize(results: list[dict]) -> dict:
    n = len(results)
    passed = sum(1 for r in results if r["passed"])
    return {"cases": n, "passed": passed, "rate": round(passed / n, 3) if n else 0.0}
