"""P0.3 regression tests: spending limits are enforced BEFORE money is spent, atomically, across
concurrent calls and nested budgets (job -> evolution run -> project; provider ceilings).

HTTP to the model provider is replaced by a local fake (`requests.post`); the reservation, settlement,
cancellation and usage-ledger code is the real code."""
import threading
import time

import pytest

from foundry import config, llm, pipeline, skills as sk, usage


class FakeHTTP:
    """Counts requests; replies with configurable usage after an optional delay."""

    def __init__(self, tin=1000, tout=1000, delay=0.0, cost=None, omit_usage=False):
        self.n, self.tin, self.tout, self.delay, self.cost, self.omit = 0, tin, tout, delay, cost, omit_usage
        self.lock = threading.Lock()

    def __call__(self, url, headers=None, json=None, timeout=None):
        with self.lock:
            self.n += 1
        time.sleep(self.delay)
        usage_ = {} if self.omit else {"prompt_tokens": self.tin, "completion_tokens": self.tout}
        if self.cost is not None:
            usage_["cost"] = self.cost

        class R:
            status_code, text = 200, ""

            @staticmethod
            def json():
                return {"choices": [{"message": {"content": "hello world"}}], "usage": usage_, "model": "m"}
        return R()


@pytest.fixture
def priced(monkeypatch, tmp_path):
    monkeypatch.setattr(usage, "PATH", tmp_path / "usage.db")
    llm.reset_process_budgets()
    monkeypatch.setitem(config.PROVIDERS["gpt"], "api_key", "k")
    monkeypatch.setitem(config.PROVIDERS["gpt"], "price_in", 1.0)    # $1 / 1M in
    monkeypatch.setitem(config.PROVIDERS["gpt"], "price_out", 10.0)  # $10 / 1M out
    http = FakeHTTP()
    monkeypatch.setattr(llm.requests, "post", http)
    yield http
    llm.reset_process_budgets()


MSG = [{"role": "user", "content": "x" * 300}]  # ~116 estimated input tokens


def test_reservation_uses_the_full_output_ceiling_not_half(priced):
    worst = llm.worst_case_usd(config.PROVIDERS["gpt"], MSG, 8000)
    assert worst >= 8000 / 1e6 * 10.0                 # the full max_tokens is priced in
    m = llm.new_meter(worst * 0.75)                   # half the output (the old rule) would have fit...
    with pytest.raises(llm.BudgetExceeded):
        llm.chat(MSG, provider="gpt", role="builder", meter=m, max_tokens=8000)
    assert priced.n == 0                               # ...but nothing is sent unless the worst case fits


def test_concurrent_calls_cannot_double_reserve(priced):
    priced.delay = 0.3
    one = llm.worst_case_usd(config.PROVIDERS["gpt"], MSG, 4000)
    m = llm.new_meter(one * 2.5)                       # room for exactly two worst cases
    out, errs = [], []

    def go():
        try:
            out.append(llm.chat(MSG, provider="gpt", role="builder", meter=m, max_tokens=4000))
        except llm.BudgetExceeded as e:
            errs.append(e)
    ts = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert priced.n == len(out) == 2 and len(errs) == 4
    assert m.reserved == 0 and m.spent <= m.cap_usd


def test_settlement_reconciles_to_actual_and_reports_three_cost_kinds(priced):
    priced.cost = 0.0042
    m = llm.new_meter(5.0, job_id="job_abc123")
    r = llm.chat(MSG, provider="gpt", role="builder", meter=m, max_tokens=2000)
    assert r["calculated_usd"] == pytest.approx(1000 / 1e6 * 1 + 1000 / 1e6 * 10)
    assert r["provider_usd"] == 0.0042 and r["usd"] == 0.0042       # provider-reported wins when present
    assert r["estimated_usd"] > r["calculated_usd"]                 # reservation was the conservative bound
    assert m.reserved == 0 and m.total_usd == pytest.approx(0.0042)
    s = m.summary()
    assert s["estimated_usd"] and s["calculated_usd"] and s["provider_reported_usd"] == 0.0042
    db = usage.summary(job_id="job_abc123")
    assert db["calls"] == 1 and db["provider_reported_usd"] == pytest.approx(0.0042)


def test_missing_provider_usage_is_charged_at_the_reservation(priced):
    priced.omit = True
    m = llm.new_meter(5.0)
    r = llm.chat(MSG, provider="gpt", role="builder", meter=m, max_tokens=2000)
    assert r["usage_source"] == "approximated" and r["usd"] == pytest.approx(r["estimated_usd"])


def test_unknown_pricing_refuses_the_call(priced, monkeypatch):
    monkeypatch.setitem(config.PROVIDERS["gpt"], "price_out", None)
    with pytest.raises(llm.PricingUnknown):
        llm.chat(MSG, provider="gpt", role="builder", meter=llm.new_meter(100.0))
    assert priced.n == 0


def test_parent_budget_caps_all_children_together(priced):
    one = llm.worst_case_usd(config.PROVIDERS["gpt"], MSG, 1000)
    run = llm.new_meter(one * 1.5, name="evolution run")
    a, b = llm.new_meter(10.0, parent=run), llm.new_meter(10.0, parent=run)  # each job alone is generous
    llm.chat(MSG, provider="gpt", role="builder", meter=a, max_tokens=1000)
    with pytest.raises(llm.BudgetExceeded, match="evolution run"):
        llm.chat(MSG, provider="gpt", role="builder", meter=b, max_tokens=1000)
    assert run.total_usd == pytest.approx(a.total_usd) and priced.n == 1


def test_project_and_provider_ceilings_survive_a_restart(priced, monkeypatch):
    llm.chat(MSG, provider="gpt", role="builder", meter=llm.new_meter(None), max_tokens=1000)
    spent = usage.total_usd()
    assert spent > 0
    monkeypatch.setenv("FOUNDRY_PROJECT_CAP_USD", str(spent + 0.0001))
    llm.reset_process_budgets()                         # "restart": the ceiling is rebuilt from usage.db
    with pytest.raises(llm.BudgetExceeded, match="project"):
        llm.chat(MSG, provider="gpt", role="builder", meter=llm.new_meter(None), max_tokens=1000)
    monkeypatch.delenv("FOUNDRY_PROJECT_CAP_USD")
    monkeypatch.setenv("FOUNDRY_PROVIDER_CAP_USD_GPT", str(spent))
    llm.reset_process_budgets()
    with pytest.raises(llm.BudgetExceeded, match="provider gpt"):
        llm.chat(MSG, provider="gpt", role="builder", meter=llm.new_meter(None), max_tokens=1000)
    assert priced.n == 1


def test_cancellation_stops_later_paid_calls(priced):
    ev = threading.Event()
    m = llm.new_meter(10.0, cancel_event=ev)
    llm.chat(MSG, provider="gpt", role="builder", meter=m, max_tokens=500)
    ev.set()
    with pytest.raises(llm.Cancelled):
        llm.chat(MSG, provider="gpt", role="builder", meter=m, max_tokens=500)
    assert priced.n == 1


def test_cancelled_build_runs_no_later_stage(sandbox):
    ev = threading.Event()
    stages = []

    def progress(stage, info=None):
        stages.append(stage)
        if stage == "tests":
            ev.set()                                     # user cancels while tests are being written
    res = pipeline.build("an agent", sk.load(), budget_usd=1.0, cancel=ev, progress=progress)
    assert res["status"] == "cancelled"
    assert "build" not in stages and "verify" not in stages
    assert "builder" not in res["cost"]["by_role"]


def test_usage_ledger_is_idempotent_and_refuses_conflicts(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "PATH", tmp_path / "u.db")
    rec = {"call_id": "c1", "role": "builder", "provider": "gpt", "model": "m", "tokens_in": 1, "tokens_out": 2,
           "usd": 0.01, "calculated_usd": 0.01, "cost_source": "calculated"}
    usage.record(rec)
    usage.record(dict(rec))                              # exact replay (retry): no double count
    assert usage.summary()["calls"] == 1 and usage.total_usd() == pytest.approx(0.01)
    with pytest.raises(usage.UsageConflict):
        usage.record({**rec, "usd": 0.5})                # same id, different facts: refused
    with pytest.raises(ValueError, match="sensitive"):
        usage.record({**rec, "call_id": "c2", "metadata": {"prompt": "secret prompt"}})


def test_evolution_run_ceiling_is_hard(sandbox):
    """The scripted model costs $0.01/call. A $0.05 run ceiling must stop the run, never be exceeded."""
    from foundry import evolve, ledger
    evolve.run(1, say=lambda *_: None, dev_limit=3, max_usd=0.05)
    spent = sum(b.get("usd") or 0 for b in ledger.read("build"))
    assert spent <= 0.05 + 1e-9
    assert not ledger.read("patch_decision") or ledger.read("patch_decision")[-1]["decision"] != "promoted"
    assert sk.current_version() == "v0000"


def test_inconclusive_evaluation_is_not_promoted():
    from foundry.roles import judge
    cur = {"_solved_set": ["a"], "tasks": 8, "usd_per_build": 0.1, "case_pass": 5, "infrastructure_failures": 0}
    cand = {"_solved_set": ["a", "b", "c"], "tasks": 8, "usd_per_build": 0.1, "case_pass": 9,
            "infrastructure_failures": 2}
    ok, why = judge.decide(cur, cand)
    assert not ok and "inconclusive" in why
