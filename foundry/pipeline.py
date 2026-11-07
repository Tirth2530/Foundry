"""One FOUNDRY build: plan -> independent tests -> build -> static gate -> execute -> repair
loop -> verified artifact. Used by the agent network, the CLI and the evaluation loops.

Evidence separates OBSERVED (executed checks, with transcripts) from INFERRED (model claims
such as the plan). Hidden benchmark tests, when present, are run once at the end and never
fed back into the repair loop.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from . import config, ledger, llm
from .harness import run as harness
from .roles import adversary, architect, builder, card_validator, examiner, integrator


def new_job_id() -> str:
    return "job_" + uuid.uuid4().hex[:6]


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data, indent=1, default=str))


def _feedback(static: dict, results: list[dict], card_problems: list, raw: dict, stderr: str, skills) -> str:
    lines = []
    for name, errs in static.items():
        lines += [f"[static:{name}] {e}" for e in errs]
    for name, tb in (raw.get("load_errors") or {}).items():
        lines.append(f"[load:{name}] {tb[-700:]}")
    for f in harness_failures(results)[:14]:
        lines.append(f"[test:{f['case']}] sent {f['send']!r} -> {f['code']}: {f['detail']}")
    for p in card_problems[:5]:
        lines.append(f"[card-audit:{p['case']}] " + "; ".join(p["errors"]))
    if skills.tool("include_reply_text"):
        for r in results[:6]:
            for t in r["turns"]:
                if not t["ok"]:
                    lines.append(f"[reply:{r['name']}] {t['reply_text'][:300]!r}")
    if stderr:
        lines.append("[runtime log]\n" + stderr[-1200:])
    return "\n".join(lines) or "unknown failure"


def harness_failures(results):
    from .dsl import failures
    return failures(results)


# ---------------------------------------------------------------- transport
# None  -> roles are called in-process (CLI, batch evaluation, "direct" mode).
# set   -> every role call is a real uAgents message to that role's agent (agent network);
#          payloads are JSON, the role agent loads the skill version itself and returns its
#          model-usage records, which are merged into this job's cost meter.
TRANSPORT = None

LLM_ROLES = {"architect", "examiner", "adversary", "builder"}


def rpc(role: str, op: str, fn, *, skills=None, meter=None, events=None, **kw):
    if TRANSPORT is None:
        if role in LLM_ROLES:
            return fn(**kw, skills=skills, meter=meter, events=events)
        return fn(**kw)
    payload = {"op": op, "kwargs": kw}
    if skills is not None:
        payload.update(skills_path=str(skills.path), skills_version=skills.version)
    if meter is not None and meter.cap_usd is not None:
        payload["cap_remaining"] = max(0.0, meter.cap_usd - meter.total_usd)
    res = TRANSPORT(role, payload)
    for c in res.get("calls", []):
        if meter is not None:
            meter.add(c)
    if events is not None:
        events.extend(res.get("events", []))
        events.append({"rpc": f"{role}.{op}", "ok": res.get("ok"), "ms": res.get("ms")})
    if not res.get("ok"):
        if res.get("budget"):
            raise llm.BudgetExceeded(res.get("error", "budget"))
        raise llm.LLMError(f"{role}.{op} failed: {res.get('error')}")
    return res.get("result")


def build(request: str, skills, *, budget_usd: float | None = None, job_id: str | None = None,
          hidden_tests: list | None = None, spec_id: str | None = None, progress=None,
          out_dir: Path | None = None) -> dict:
    job_id = job_id or new_job_id()
    jd = out_dir or (config.RUNS / job_id)
    meter = llm.CostMeter(cap_usd=budget_usd)
    events: list = []
    say = progress or (lambda stage, info=None: None)
    t0 = time.time()
    result = {"job_id": job_id, "spec_id": spec_id, "skills_version": skills.version, "request": request,
              "status": "running", "attempts": [], "events": events}
    _write(jd / "request.txt", request)
    try:
        say("plan")
        plan = rpc("architect", "plan", architect.plan, skills=skills, meter=meter, events=events, request=request)
        _write(jd / "plan.json", plan)
        result["plan"] = plan

        say("tests")
        exam = rpc("examiner", "write_tests", examiner.write_tests, skills=skills, meter=meter, events=events,
                   request=request) if skills.tool("run_examiner_tests") \
            else {"cases": [], "provider": None, "independent": False}
        adv = rpc("adversary", "cases", adversary.cases, skills=skills, meter=meter, events=events, request=request)
        verify_cases = exam["cases"] + adv
        _write(jd / "examiner_tests.json", exam)
        _write(jd / "adversary_tests.json", adv)
        result["examiner"] = {"provider": exam.get("provider"), "independent": exam.get("independent"),
                              "cases": len(exam["cases"]), "dropped": len(exam.get("dropped", []))}

        say("build", {"tests": len(exam["cases"]), "provider": exam.get("provider"),
                      "independent": exam.get("independent"), "adversarial": len(adv)})
        art = rpc("builder", "build", builder.build, skills=skills, meter=meter, events=events, request=request, plan=plan)
        budget = int(skills.config.get("repair_budget", 2))
        verify_results: list = []
        for attempt in range(budget + 1):
            ad = jd / "attempts" / f"{attempt:02d}"
            for a in art["agents"]:
                _write(ad / "agents" / f"{a['name']}.py", a["code"])
            static = rpc("integrator", "check", integrator.check, build=art) if skills.tool("preflight_static") else {}
            rec = {"attempt": attempt, "static": static, "provider": art.get("provider")}
            if static and attempt < budget:
                rec["skipped_run"] = True
                fb = _feedback(static, [], [], {}, "", skills)
            else:
                say("verify", {"attempt": attempt})
                run = rpc("runner", "run", harness.run, agents=art["agents"], entry=art["entry"], cases=verify_cases)
                verify_results = run["results"]
                audit = rpc("card_validator", "audit", card_validator.audit, raw=run["raw"])
                summ = harness.summarize(verify_results)
                rec.update(verify=summ, card_audit=audit, load_errors=run["raw"].get("load_errors"))
                _write(ad / "verify.json", {"results": verify_results, "raw": run["raw"], "stderr": run["stderr_tail"]})
                fb = _feedback(static, verify_results, audit, run["raw"], run["stderr_tail"], skills)
                if summ["passed"] == summ["cases"] and not audit and not static:
                    result["attempts"].append(rec)
                    break
            result["attempts"].append(rec)
            if attempt < budget:
                say("repair", {"attempt": attempt + 1, "why": fb[:300]})
                _write(ad / "feedback.txt", fb)
                art = rpc("builder", "repair", builder.repair, skills=skills, meter=meter, events=events,
                          request=request, plan=plan, prev=art, feedback=fb)
        result["artifact"] = {"entry": art["entry"], "agents": [a["name"] for a in art["agents"]]}
        result["verify"] = harness.summarize(verify_results) if verify_results else {"cases": 0, "passed": 0, "rate": 0}
        _write(jd / "final" / "build.json", art)
        for a in art["agents"]:
            _write(jd / "final" / "agents" / f"{a['name']}.py", a["code"])

        if hidden_tests is not None:
            say("hidden")
            hid = rpc("runner", "run", harness.run, agents=art["agents"], entry=art["entry"], cases=hidden_tests)
            hs = harness.summarize(hid["results"])
            result["hidden"] = {**hs, "solved": hs["passed"] == hs["cases"],
                                "failures": harness_failures(hid["results"])}
            _write(jd / "hidden.json", {"results": hid["results"], "stderr": hid["stderr_tail"]})
        result["status"] = "verified" if result["verify"]["cases"] and \
            result["verify"]["passed"] == result["verify"]["cases"] else "unverified"
    except llm.BudgetExceeded as e:
        result["status"] = "budget_exceeded"
        result["error"] = str(e)
    except Exception as e:  # noqa: BLE001 - recorded as evidence, surfaced by Recovery
        result["status"] = "error"
        result["error"] = f"{type(e).__name__}: {e}"
    result["cost"] = meter.summary()
    result["seconds"] = round(time.time() - t0, 1)
    result["evidence"] = observed_vs_inferred(result)
    _write(jd / "result.json", result)
    rpc("ledger", "append", ledger.append, kind="build", job_id=job_id, spec_id=spec_id, skills_version=skills.version,
                  status=result["status"], verify=result.get("verify"),
                  hidden={k: v for k, v in (result.get("hidden") or {}).items() if k != "failures"} or None,
                  usd=result["cost"]["total_usd"], seconds=result["seconds"], attempts=len(result["attempts"]))
    return result


def observed_vs_inferred(r: dict) -> dict:
    v = r.get("verify") or {}
    observed = []
    if v.get("cases"):
        observed.append(f"{v['passed']}/{v['cases']} independent test cases passed when executed over ACP")
    if r.get("examiner"):
        e = r["examiner"]
        observed.append(f"tests written by {e.get('provider')} from the request only "
                        f"({'different model family from builder' if e.get('independent') else 'same family as builder'})")
    if r.get("attempts"):
        observed.append(f"{len(r['attempts'])} build attempt(s); each attempt's code and transcripts are saved")
    if r.get("hidden"):
        h = r["hidden"]
        observed.append(f"hidden benchmark: {h['passed']}/{h['cases']} cases")
    inferred = []
    if r.get("plan"):
        inferred.append("system design (Architect plan) is a model proposal, not verified behaviour")
    inferred.append("behaviour on inputs outside the executed tests is not proven")
    return {"observed": observed, "inferred": inferred}
