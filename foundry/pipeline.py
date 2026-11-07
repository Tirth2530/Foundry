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

from . import config, contract as contract_mod, ledger, llm, topology as topo, trace as trace_mod
from .harness import run as harness
from .roles import adversary, architect, builder, card_validator, examiner, integrator


INFRA_MARKERS = ("SANDBOX UNAVAILABLE", "InfrastructureError", "HTTP 401", "HTTP 403", "invalid_api_key",
                 "insufficient_quota", "No model API keys", "Docker", "Connection refused", "all providers failed")


def is_infrastructure(err: str) -> bool:
    return any(m in (err or "") for m in INFRA_MARKERS)


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

LLM_ROLES = {"scout", "architect", "examiner", "adversary", "builder"}
COLLAB_OK = {topo.PASS, topo.NOT_TESTED}  # NOT TESTED only ever means "single agent, nothing required"


def rpc(role: str, op: str, fn, *, skills=None, meter=None, events=None, **kw):
    """Call a role (in-process or as a uAgents message). Every call is a span in the job's trace."""
    with trace_mod.span(f"{role}.{op}", transport="agent-network" if TRANSPORT else "in-process") as attrs:
        before = meter.total_usd if meter is not None else None
        out = _rpc(role, op, fn, skills=skills, meter=meter, events=events, **kw)
        if meter is not None:
            attrs["usd"] = round(meter.total_usd - before, 6)
        if role == "runner" and isinstance(out, dict):
            raw = out.get("raw") or {}
            attrs.update(sandbox=out.get("sandbox"), cases=len(out.get("results") or []),
                         passed=sum(1 for r in out.get("results") or [] if r.get("passed")),
                         inter_agent_processed=len([r for r in raw.get("received_log") or []
                                                    if r.get("from") != "client" and r.get("ok")]))
        return out


def _rpc(role: str, op: str, fn, *, skills=None, meter=None, events=None, **kw):
    if TRANSPORT is None:
        if role in LLM_ROLES:
            return fn(**kw, skills=skills, meter=meter, events=events)
        return fn(**kw)
    payload = {"op": op, "kwargs": kw}
    if skills is not None:
        payload.update(skills_path=str(skills.path), skills_version=skills.version)
    grant = None
    if meter is not None and role in LLM_ROLES | {"miner"}:
        # The role agent spends on our behalf: reserve its whole allowance in OUR budget chain first, so
        # concurrent jobs/role calls can never jointly overspend. The role agent's own meter is capped at this
        # allowance. Afterwards the grant is released AND the reported usage committed in one atomic step.
        meter.raise_if_cancelled()
        room = meter.headroom()
        if room is not None and room <= 0:
            raise llm.BudgetExceeded(f"no budget left for {role}.{op}")
        grant = meter.reserve(room if room is not None else 0.0)
        payload["cap_remaining"] = room
    res = None
    try:
        res = TRANSPORT(role, payload)
    finally:
        reported = res.get("calls") if isinstance(res, dict) and "calls" in res else None
        if grant is not None:
            meter.settle_grant(grant, reported)
        elif reported and meter is not None:  # a non-LLM role reported spend: book it, never drop it
            meter.settle_grant(meter.reserve(0.0), reported)
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
          out_dir: Path | None = None, topology: dict | None = None, parent_meter=None, cancel=None,
          contract: dict | None = None) -> dict:
    """parent_meter: an enclosing budget (evolution run, comparison arm); cancel: threading.Event that stops
    every later paid stage (checked before each stage and before each model call); contract: the user's sealed,
    approved topology contract (foundry.contract). Without one, it is derived from the request text."""
    job_id = job_id or new_job_id()
    jd = out_dir or (config.RUNS / job_id)
    tracer = trace_mod.current() if (trace_mod.current() and trace_mod.current().trace_id == job_id) else \
        trace_mod.Tracer(job_id, jd / "trace.jsonl")
    with trace_mod.activate(tracer), tracer.span("build", skills=skills.version, spec=spec_id,
                                                 budget_usd=budget_usd) as attrs:
        res = _build(request, skills, budget_usd=budget_usd, job_id=job_id, hidden_tests=hidden_tests,
                     spec_id=spec_id, progress=progress, jd=jd, topology=topology, parent_meter=parent_meter,
                     cancel=cancel, contract=contract)
        attrs.update(status=res["status"], usd=res["cost"]["total_usd"], attempts=len(res["attempts"]),
                     collaboration=(res.get("collaboration") or {}).get("status"))
        return res


def _build(request, skills, *, budget_usd, job_id, hidden_tests, spec_id, progress, jd, topology,
           parent_meter, cancel, contract) -> dict:
    meter = llm.new_meter(budget_usd, parent=parent_meter, name=f"build {job_id}", job_id=job_id, cancel_event=cancel)
    events: list = []
    _say = progress or (lambda stage, info=None: None)

    tracer = trace_mod.current()

    def say(stage, info=None):
        meter.raise_if_cancelled()  # cancellation checkpoint between stages
        tracer.event(f"stage:{stage}", **{k: v for k, v in (info or {}).items() if k != "why"})
        _say(stage, info)
    t0 = time.time()
    result = {"job_id": job_id, "spec_id": spec_id, "skills_version": skills.version, "request": request,
              "status": "running", "attempts": [], "events": events}
    _write(jd / "request.txt", request)
    try:
        say("plan")
        # The USER's topology contract is fixed BEFORE any model designs anything. It comes from the approved,
        # sealed contract (Foreman) or, for CLI/benchmark runs, from the request text itself (no model involved).
        if contract is not None:
            contract_mod.check_sealed(contract)
        else:
            contract = contract_mod.derive(request, source="request text (CLI or benchmark run)")
        result["contract"] = contract
        _write(jd / "contract.json", contract or {"multi_agent": False})
        plan = rpc("architect", "plan", architect.plan, skills=skills, meter=meter, events=events, request=request)
        plan_n = len([a for a in (plan or {}).get("agents", []) if isinstance(a, dict)])
        if contract and plan_n < contract["min_agents"]:
            result["plan_conflict"] = (f"Architect proposed {plan_n} agent(s); your contract requires at least "
                                       f"{contract['min_agents']}. The contract is enforced, not the plan.")
        # precedence: benchmark spec topology > user contract > Architect plan (fallback only)
        required_base = topology or contract_mod.requirement(contract)
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
        meter.raise_if_cancelled()
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
                result["sandbox"] = run.get("sandbox")
                result["runtime"] = {"uagents": run["raw"].get("uagents_version"), "python": run["raw"].get("python")}
                audit = rpc("card_validator", "audit", card_validator.audit, raw=run["raw"])
                names = [a["name"] for a in art["agents"]]
                collab = topo.evaluate(run["raw"], names, art["entry"],
                                       required_base or topo.required_from_plan(plan, names), verify_results)
                rec["collaboration"] = collab
                rec["traffic"] = run["raw"].get("traffic")
                rec["traffic_tap"] = run["raw"].get("traffic_tap")
                summ = harness.summarize(verify_results)
                rec.update(verify=summ, card_audit=audit, load_errors=run["raw"].get("load_errors"))
                _write(ad / "verify.json", {"results": verify_results, "raw": run["raw"], "stderr": run["stderr_tail"]})
                fb = _feedback(static, verify_results, audit, run["raw"], run["stderr_tail"], skills)
                fb += "".join(f"\n[collaboration] {c['detail']}" for c in collab["failures"])
                if summ["passed"] == summ["cases"] and not audit and not static and collab["status"] in COLLAB_OK:
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
            tfails = topo.check_spec(topology, hid["raw"], [a["name"] for a in art["agents"]], art["entry"],
                                     hid["results"])
            result["hidden"] = {**hs, "solved": hs["passed"] == hs["cases"] and not tfails,
                                "failures": harness_failures(hid["results"]) + tfails,
                                "topology": {"required": topology, "traffic": hid["raw"].get("traffic"),
                                             "observable": hid["raw"].get("traffic_tap"), "failures": tfails}
                                if topology else None}
            _write(jd / "hidden.json", {"results": hid["results"], "stderr": hid["stderr_tail"]})
        # RELEASE GATE: every check, on the FINAL artifact, regardless of any tool setting
        final_static = rpc("integrator", "check", integrator.check, build=art)
        last = result["attempts"][-1] if result["attempts"] else {}
        exam_names = {c.get("name") for c in exam["cases"]}
        exam_passed = sum(1 for r in verify_results if r["name"] in exam_names and r["passed"])
        gate = {
            "independent_tests_ran": bool(exam["cases"]),
            "all_tests_passed": bool(result["verify"]["cases"]) and result["verify"]["passed"] == result["verify"]["cases"],
            "examiner_tests_passed": f"{exam_passed}/{len(exam['cases'])}",
            "static_clean": not final_static,
            "cards_clean": not last.get("card_audit") and not last.get("skipped_run"),
            "no_load_errors": not last.get("load_errors"),
            # FAIL CLOSED: a multi-agent build needs OBSERVED collaboration; missing observability = UNVERIFIED
            "agents_collaborate": (last.get("collaboration") or {}).get("status") in COLLAB_OK,
        }
        collab_final = last.get("collaboration") or {"status": topo.UNVERIFIED, "failures": [],
                                                     "evidence": {"reason": "final attempt was never executed"}}
        result["collaboration"] = collab_final
        if collab_final["status"] == topo.UNVERIFIED:
            result["verify_note"] = "inter-agent traffic not observable: collaboration UNVERIFIED, so the build is not verified"
        result["release_gate"] = {**gate, "collaboration_status": collab_final["status"], "static": final_static,
                                  "sandbox": result.get("sandbox")}
        tracer.event("release_gate", **{k: v for k, v in gate.items()}, collaboration=collab_final["status"])
        ok = all(v for k, v in gate.items() if k != "examiner_tests_passed") and exam_passed == len(exam["cases"])
        result["status"] = "verified" if ok else "unverified"
    except llm.BudgetExceeded as e:
        result["status"] = "budget_exceeded"
        result["error"] = str(e)
    except llm.Cancelled as e:
        result["status"] = "cancelled"
        result["error"] = str(e)
    except Exception as e:  # noqa: BLE001 - recorded as evidence, surfaced by Recovery
        result["status"] = "error"
        result["error"] = f"{type(e).__name__}: {e}"
        result["error_kind"] = "infrastructure" if is_infrastructure(result["error"]) else "build"
    result["cost"] = meter.summary()
    result["seconds"] = round(time.time() - t0, 1)
    result["evidence"] = observed_vs_inferred(result)
    _write(jd / "result.json", result)
    rpc("ledger", "append", ledger.append, kind="build", job_id=job_id, spec_id=spec_id, skills_version=skills.version,
                  status=result["status"], verify=result.get("verify"),
                  hidden={k: v for k, v in (result.get("hidden") or {}).items() if k != "failures"} or None,
                  usd=result["cost"]["total_usd"], seconds=result["seconds"], attempts=len(result["attempts"]),
                  collaboration=(result.get("collaboration") or {}).get("status"),
                  contract_sha256=(result.get("contract") or {}).get("sha256"),
                  code_sha256=_code_sha(jd))
    if hidden_tests is None and (jd / "final" / "agents").exists():
        try:  # user builds get a Proof Passport (benchmark builds are summarised by the judge instead)
            from . import passport
            result["passport"] = str(passport.write(job_id, jd))
        except Exception as e:  # noqa: BLE001 - a passport problem must never hide the build result
            result["passport_error"] = f"{type(e).__name__}: {e}"
    return result


def _code_sha(jd: Path) -> str | None:
    if not (jd / "final" / "agents").exists():
        return None
    from .passport import code_digest
    return code_digest(jd)["system_sha256"]


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
