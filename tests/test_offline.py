"""Offline tests: run anywhere, no API keys, no uagents needed.

They exercise the real validators and the real orchestration (pipeline, repair loop,
evolution loop, sealed promotion gate, rollback) with a scripted model and a scripted
execution runner, so the control logic is proven before any credits are spent.
"""
import json
import shutil

import pytest

from foundry import bench, cards, config, dsl, ledger, llm, pipeline, skills as sk, static_check
from foundry.harness import run as harness
from foundry.roles import examiner, integrator, judge, quartermaster


# ------------------------------------------------------------------ pure validators

def test_card_validator_accepts_builder_output():
    md = cards.review("Deploy?", [("Service", "api")], ("Approve", {"action": "approve"}), ("Reject", {"action": "reject"}))
    assert cards.validate(md) == []
    for md in (cards.form("F", [{"name": "value", "kind": "number", "label": "V"}], "Go", {"action": "x"}),
               cards.carousel("C", "s", [{"title": "a"}, {"title": "b"}]),
               cards.detail("D", [("a", "b")], [("Go", {"action": "x"}, True)])):
        assert cards.validate(md) == [], cards.validate(md)


def test_card_validator_catches_real_mistakes():
    bad = {"card_protocol_version": 1, "requires_card_interaction": "true", "card_kind": "review",
           "card_payload": json.dumps({"title": "x", "summary_rows": [], "buttons": []})}
    errs = " ".join(cards.validate(bad))
    assert "must be a string" in errs and "approve_cta" in errs and "card_protocol_version" in errs
    sel = cards.form("F", [{"name": "u", "kind": "select", "label": "U"}], "Go", {})
    assert any("options" in e for e in cards.validate(sel))
    wrapped = cards.metadata("carousel", {"data": {"items": []}})
    assert any("extra layer" in e for e in cards.validate(wrapped))


def test_dsl_judges_transcripts():
    case = {"name": "c", "turns": [{"send": "5 km", "checks": [
        {"type": "ack"}, {"type": "text_number", "value": 3.11, "tol": 0.01}, {"type": "no_card"}]}]}
    good = {"turns": [{"acked": True, "replies": [{"texts": ["5 km = 3.11 mi"], "metadata": [], "end_session": False}]}]}
    assert dsl.evaluate_case(case, good)["passed"]
    silent = {"turns": [{"acked": False, "replies": []}]}
    r = dsl.evaluate_case(case, silent)
    assert not r["passed"] and {f["code"] for f in dsl.failures([r])} == {"NO_REPLY", "NO_ACK"}
    card_case = {"name": "k", "turns": [{"send": "x", "checks": [{"type": "card", "kind": "form", "fields": ["unit"]}]}]}
    md = cards.form("F", [{"name": "value", "kind": "number", "label": "V"}], "Go", {})
    rec = {"turns": [{"acked": True, "replies": [{"texts": ["Which unit?"], "metadata": [md], "end_session": False}]}]}
    f = dsl.failures([dsl.evaluate_case(card_case, rec)])
    assert f and f[0]["code"] == "CARD_WRONG" and "unit" in f[0]["detail"]


def test_static_check_enforces_hosted_contract():
    ok = open(config.ROOT / "tools" / "reference" / "good_agent.py").read()
    assert static_check.check(ok) == []
    bad = "import requests\nfrom uagents import Agent\nagent = Agent(name='x')\nagent.run()\n"
    errs = " ".join(static_check.check(bad))
    assert "requests" in errs and "construct Agent" in errs and "agent.run" in errs
    sysbad = integrator.check({"entry": "main", "agents": [
        {"name": "main", "code": ok.replace("agent.include", "x = PEERS['ghost']\nagent.include")}]})
    assert "ghost" in json.dumps(sysbad)


def test_extract_agents_multi():
    text = "intro\n```python name=parser\nprint(1)\n```\n```python name=coordinator entry\nprint(2)\n```"
    out = llm.extract_agents(text)
    assert out["entry"] == "coordinator" and [a["name"] for a in out["agents"]] == ["parser", "coordinator"]
    assert llm.extract_json('noise ```json\n{"a": 1}\n``` tail') == {"a": 1}


def test_benchmark_is_well_formed_and_split():
    rep = bench.lint_all()
    assert len(rep) == 35 and not any(rep.values())   # 32 original + 3 coupled engineering tasks
    ids = [s["id"] for split in ("dev",) for s in bench._load_dir(bench.DIRS[split])]
    assert len(ids) == 16
    assert "tests" not in bench.public_view(bench._load_dir(bench.DIRS["dev"])[0])


def test_judge_rule_is_strict():
    cur = {"_solved_set": ["a", "b"], "tasks": 8, "usd_per_build": 0.1, "case_pass": 10}
    assert judge.decide(cur, {"_solved_set": ["a", "b", "c"], "tasks": 8, "usd_per_build": 0.1, "case_pass": 12})[0]
    lost = judge.decide(cur, {"_solved_set": ["a", "c", "d"], "tasks": 8, "usd_per_build": 0.1, "case_pass": 20})
    assert not lost[0] and "regression" in lost[1]
    assert judge.decide(cur, {"_solved_set": ["a", "b"], "tasks": 8, "usd_per_build": 0.08, "case_pass": 10})[0]
    assert not judge.decide(cur, {"_solved_set": ["a", "b"], "tasks": 8, "usd_per_build": 0.095, "case_pass": 11})[0]


# ------------------------------------------------------------------ orchestration with scripted model + runner

def test_pipeline_build_records_observed_vs_inferred(sandbox):
    s = sk.load()
    res = pipeline.build("an agent", s, budget_usd=1.0, hidden_tests=[
        {"name": "h1", "turns": [{"send": "x", "checks": []}]}], spec_id="t")
    assert res["status"] == "verified" and res["hidden"]["solved"] is False
    assert res["cost"]["total_usd"] > 0 and res["evidence"]["observed"] and res["evidence"]["inferred"]
    assert (config.RUNS / res["job_id"] / "final" / "agents" / "main.py").exists()
    assert ledger.read("build")[-1]["job_id"] == res["job_id"]


def test_budget_cap_stops_work(sandbox):
    res = pipeline.build("an agent", sk.load(), budget_usd=0.025)
    assert res["status"] == "budget_exceeded" and res["cost"]["total_usd"] <= 0.04


def test_evolution_promotes_a_real_fix_and_rejects_noise(sandbox, monkeypatch):
    from foundry import evolve
    log = []
    evolve.run(1, say=log.append)
    assert sk.current_version() == "v0001", log
    d = ledger.read("patch_decision")[-1]
    assert d["decision"] == "promoted" and d["candidate_eval"]["solved"] > d["current"]["solved"]
    assert "010-review-cards.md" in [n for n, _ in sk.load().lessons]
    ok, n = ledger.verify_chain()
    assert ok and n > 10
    # a second round: the evolver proposes a useless patch -> must be rejected, CURRENT unchanged
    monkeypatch.setattr(llm, "chat", _useless_evolver(llm.chat))
    evolve.run(1, say=log.append)
    last = ledger.read("patch_decision")[-1]
    assert last["decision"] == "rejected" and last["candidate"] == "c0002", log
    assert sk.current_version() == "v0001"  # rejected candidate discarded = rollback
    sk.rollback("v0000")
    assert sk.current_version() == "v0000"


def _useless_evolver(inner):
    def chat(messages, *, provider, role, meter=None, **kw):
        if role == "evolver":
            return {"text": json.dumps({"kind": "lesson_add", "file": "020-noise.md", "content": "Be nice.",
                                        "rationale": "noise"}), "role": role, "provider": provider, "model": "f",
                    "fell_back": False, "tokens_in": 1, "tokens_out": 1, "usd": 0.0, "t": 0}
        return inner(messages, provider=provider, role=role, meter=meter, **kw)
    return chat


def test_quartermaster_quotes_from_history(sandbox):
    q0 = quartermaster.quote("an agent", sk.load())
    assert q0["basis"].startswith("token model") and q0["cap_usd"] >= q0["estimate_usd"]
    pipeline.build("an agent", sk.load(), budget_usd=1.0)
    q1 = quartermaster.quote("an agent", sk.load())
    assert "measured" in q1["basis"] and q1["price_usd"] >= config.MIN_CHARGE_USD
    r = quartermaster.reconcile(q1, 0.05)
    assert r["within_cap"] in (True, False) and "delta_pct" in r


def test_benchmark_manifest_and_leakage_checks(tmp_path, monkeypatch):
    rep = bench.integrity()
    assert rep["errors"] == [], rep["errors"]                       # frozen manifest matches, no leakage
    assert rep["multi_agent"]["final"] >= 1                          # final comparison includes coupled systems
    d = tmp_path / "bench"
    shutil.copytree(config.BENCH, d)
    monkeypatch.setattr(bench, "DIRS", {"dev": d / "dev", "promotion": d / "sealed" / "promotion",
                                        "final": d / "sealed" / "final"})
    monkeypatch.setattr(bench, "MANIFEST", d / "MANIFEST.json")
    leak = json.loads((d / "dev" / "d01_isbn13.json").read_text())
    leak["id"] = "p99_leak"
    (d / "sealed" / "promotion" / "p99_leak.json").write_text(json.dumps(leak))  # a dev task copied into sealed
    errs = " | ".join(bench.integrity()["errors"])
    assert "identical task" in errs and "differ from MANIFEST" in errs
