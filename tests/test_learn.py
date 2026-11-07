import json
"""Live self-improvement from one build, and the controlled final evaluation."""
from foundry import evolve, ledger, pipeline, skills as sk


def test_learn_from_one_build_promotes_a_generalising_fix(sandbox):
    sandbox["strict"] = True
    res = pipeline.build("deployment approval agent with a review card", sk.load(), budget_usd=2.0)
    assert res["status"] == "unverified"  # the independent verifier caught the bad card on every attempt
    fails = evolve.job_failures(res["job_id"])
    assert fails and {f["code"] for f in fails} == {"CARD_INVALID"}
    log = []
    d = evolve.learn_from_job(res["job_id"], say=log.append)
    assert d["decision"] == "promoted" and d["origin"].startswith("live build")
    assert d["candidate_eval"]["solved"] > d["current"]["solved"]  # judged on the sealed set, not this job
    assert sk.current_version() == "v0001"
    again = pipeline.build("deployment approval agent with a review card", sk.load(), budget_usd=2.0)
    assert again["status"] == "verified"  # the next build benefits


def test_learn_with_no_caught_defects_changes_nothing(sandbox):
    res = pipeline.build("an agent", sk.load(), budget_usd=1.0)
    assert evolve.learn_from_job(res["job_id"], say=lambda *_: None) is None
    assert sk.current_version() == "v0000"


def test_final_eval_is_controlled_and_counted(sandbox, monkeypatch):
    evolve.run(1, say=lambda *_: None)
    assert sk.current_version() == "v0001"
    out = evolve.evaluate_final(repeats=1, say=lambda *_: None)
    assert set(out) == {"v0000", "v0001"}
    assert out["v0001"]["solved"] > out["v0000"]["solved"]
    assert out["v0000"]["routing"] == out["v0001"]["routing"]  # same models: lesson-only change
    assert len(ledger.read("final_set_opened")) == 2  # one opening per arm, each recorded


def test_ablation_arm_when_evolution_changed_models_or_budget(sandbox):
    base = sk.load()
    lesson = sk.make_candidate(base, {"kind": "lesson_add", "file": "010-review-cards.md",
                                      "content": "Review cards need approve_cta and reject_cta."}, "c9001")
    v1 = sk.load(sk.promote(lesson))
    cfg = sk.make_candidate(v1, {"kind": "config", "config_changes": {"repair_budget": 4}}, "c9002")
    sk.promote(cfg)
    out = evolve.evaluate_final(repeats=1, say=lambda *_: None)
    ablation = [v for v in out if v.endswith("lessons-only")]
    assert ablation and out[ablation[0]]["repair_budget"] == 2 and out[ablation[0]]["lessons"] == 2


def test_environment_failures_are_never_learned_from(sandbox, monkeypatch):
    import pytest
    from foundry.harness import run as harness
    monkeypatch.setattr(harness, "sandbox_mode", lambda: (_ for _ in ()).throw(
        harness.InfrastructureError("SANDBOX UNAVAILABLE: Docker Desktop is not running")))
    with pytest.raises(SystemExit) as e:
        evolve.run(1, say=lambda *_: None)
    assert "PREFLIGHT FAILED" in str(e.value) and not ledger.read("build")  # nothing spent


def test_infra_errors_mid_round_abort_without_a_patch(sandbox, monkeypatch):
    from foundry.harness import run as harness

    def boom(*a, **k):
        raise harness.InfrastructureError("SANDBOX UNAVAILABLE: Docker Desktop is not running")
    monkeypatch.setattr(evolve, "preflight", lambda say=print: None)
    monkeypatch.setattr(harness, "run", boom)
    evolve.run(1, say=lambda *_: None)
    assert ledger.read("round_aborted") and not ledger.read("patch_proposed")
    assert sk.current_version() == "v0000"


def test_final_comparison_reports_separate_metrics_with_sample_sizes(sandbox):
    from foundry import evolve as ev
    ev.run(1, say=lambda *_: None)
    out = ev.evaluate_final(repeats=2, say=lambda *_: None)
    a, c = out["v0000"], out["v0001"]
    assert a["arm"] == "A baseline" and c["arm"] == "C full evolved"
    m = c["metrics"]
    for k in ("success_rate", "success_rate_by_repeat", "success_rate_spread", "usd_per_successful_build",
              "median_seconds", "incorrect_verification_rate", "regression_rate", "inter_agent_success_rate"):
        assert k in m, k
    assert len(m["success_rate_by_repeat"]) == 2 and m["builds"] == 2 * m["tasks"]
    assert "small sample" in m["sample_note"]                       # 9 tasks x 2 repeats: says so
    assert m["regressions_vs_baseline"] == [] and m["gained_vs_baseline"]
    assert "score" not in json.dumps(m).lower()                     # no invented composite score
    assert a["budget_usd_per_build"] == c["budget_usd_per_build"]   # equal budget per build
    rec = ledger.read("final_eval")[-1]["results"]
    assert all("_builds" not in arm and "_solved_set" not in arm for arm in rec.values())  # rows stay sealed


def test_learning_records_transfer_to_different_tasks(sandbox):
    sandbox["strict"] = True
    res = pipeline.build("deployment approval agent with a review card", sk.load(), budget_usd=2.0)
    d = evolve.learn_from_job(res["job_id"], say=lambda *_: None)
    t = d["transfer"]
    assert t["identical_tasks"] == 0 and t["evaluation_tasks"] == 10 and t["max_request_similarity"] < 0.6
    assert d["equal_budget"]["routing_changed"] is False
