"""Shared fixtures. `sandbox` isolates every path FOUNDRY writes to and replaces the model and
the execution runner with scripted fakes, so orchestration logic is tested for $0."""
import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:  # use real uagents when installed, otherwise a faithful-enough stub for logic tests
    import uagents  # noqa: F401
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent / "stubs"))

from foundry import config, ledger, llm, pipeline, skills as sk  # noqa: E402
from foundry.harness import run as harness  # noqa: E402
from foundry.roles import examiner, judge  # noqa: E402


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    runs, ev, skd = tmp_path / "runs", tmp_path / "evidence", tmp_path / "skills"
    shutil.copytree(config.SKILLS / "versions" / "v0000", skd / "versions" / "v0000")
    (skd / "CURRENT").write_text("v0000\n")
    monkeypatch.setattr(config, "RUNS", runs)
    monkeypatch.setattr(config, "EVIDENCE", ev)
    monkeypatch.setattr(config, "PARALLEL_BUILDS", 2)
    monkeypatch.setattr(sk, "VERSIONS", skd / "versions")
    monkeypatch.setattr(sk, "CANDIDATES", skd / "candidates")
    monkeypatch.setattr(sk, "CURRENT", skd / "CURRENT")
    monkeypatch.setattr(ledger, "PATH", ev / "ledger.jsonl")
    monkeypatch.setattr(examiner, "CACHE", runs / "_cache")
    monkeypatch.setattr(judge, "SEALED_LOG", ev / "sealed")
    monkeypatch.setattr(pipeline, "TRANSPORT", None)
    for p in config.PROVIDERS.values():
        monkeypatch.setitem(p, "api_key", "test")

    calls = {"n": 0}

    def fake_chat(messages, *, provider, role, meter=None, **kw):
        calls["n"] += 1
        sys_txt = messages[0]["content"]
        if role == "architect":
            text = '{"agents":[{"name":"main","role":"does it"}],"entry":"main"}'
        elif role == "examiner":
            text = '{"cases":[{"name":"e1","turns":[{"send":"hi","checks":[{"type":"ack"}]}]}]}'
        elif role == "adversary":
            text = '{"messages":["edge case"]}'
        elif role in ("builder", "repair"):
            knows = "approve_cta" in sys_txt
            text = "```python name=main entry\n# " + ("approve_cta" if knows else "buttons") + \
                   "\nChatAcknowledgement\nagent.include(p)\n```"
        elif role == "scout":
            import re as _re
            ids = _re.findall(r"id=(\S+)", messages[1]["content"])
            text = json.dumps({"best": ids[0] if ids else None, "confidence": 0.9, "reason": "same job", "gaps": []})
        elif role == "miner":
            text = '{"root_cause":"review cards use wrong keys","generalizes_because":"every review card"}'
        elif role == "evolver":
            text = json.dumps({"kind": "lesson_add", "file": "010-review-cards.md",
                               "content": "Review cards need approve_cta and reject_cta objects.",
                               "rationale": "fix CARD_INVALID", "targets": "CARD_INVALID"})
        else:
            text = "OK"
        rec = {"role": role, "provider": provider, "model": "fake", "fell_back": False,
               "tokens_in": 1000, "tokens_out": 500, "usd": 0.01, "t": 0}
        if meter:
            meter.check()
            meter.add(rec)
        return {"text": text, **rec}

    def fake_run(agents, entry, cases, **kw):
        good = "approve_cta" in agents[0]["code"]
        results = []
        for c in cases:
            ok = (good or c["name"].startswith(("e1", "adv"))) and c["name"] != "two_lines"  # one dev task never passes
            checks = [{"type": "card", "ok": ok, "code": "" if ok else "CARD_INVALID",
                       "detail": "" if ok else "review missing 'approve_cta' (found keys: ['buttons'])"}]
            results.append({"name": c["name"], "passed": ok,
                            "turns": [{"send": c["turns"][0]["send"], "ok": ok, "checks": checks, "reply_text": "", "cards": 1}]})
        return {"raw": {"cases": [], "load_errors": {}}, "results": results, "stderr_tail": ""}

    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(harness, "run", fake_run)
    return calls


