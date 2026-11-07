"""Scout: reuse only what passes a live re-check; search is a hint and may fail."""
import json

from foundry import config, ledger, llm, skills as sk
from foundry.roles import scout

REQ = "ISBN-13 checker agent that replies VALID or INVALID for an ISBN-13 number"


class FakeAV:
    def __init__(self, reply="VALID", search=None, search_error=False):
        self.reply, self._search, self.err, self.invoked = reply, search or [], search_error, []

    def search(self, text, limit=10):
        if self.err:
            raise RuntimeError("search API not production ready")
        return self._search

    def invoke(self, target, message, wait_s=150):
        self.invoked.append((target, message))
        return {"texts": [self.reply] if self.reply else [], "cards": [], "acked": True, "latency_s": 9.1}


def _seed_catalog():
    jid = "job_abc123"
    d = config.RUNS / jid
    d.mkdir(parents=True)
    (d / "examiner_tests.json").write_text(json.dumps({"cases": [{"name": "v", "turns": [
        {"send": "978-0-306-40615-7", "checks": [{"type": "ack"}, {"type": "text_contains", "value": "VALID"}]}]}]}))
    ledger.append("publish", job_id=jid, agents={"main": "agent1qcatalog"}, entry="agent1qcatalog",
                  request="ISBN-13 checker: reply VALID or INVALID for ISBN-13 numbers", live_passed=True)


def test_reuses_catalog_agent_after_live_pass(sandbox):
    _seed_catalog()
    av = FakeAV(reply="VALID")
    res = scout.find(REQ, sk.load(), llm.CostMeter(), [], client=av)
    assert res["found"] and res["candidate"]["source"] == "foundry_catalog"
    assert av.invoked == [("agent1qcatalog", "978-0-306-40615-7")]  # its ORIGINAL independent test
    assert ledger.read("scout_check")[-1]["passed"] is True


def test_refuses_agent_that_fails_live_recheck(sandbox):
    _seed_catalog()
    res = scout.find(REQ, sk.load(), llm.CostMeter(), [], client=FakeAV(reply="I am a weather bot"))
    assert not res["found"] and res["live"]["passed"] is False
    assert ledger.read("scout_check")[-1]["passed"] is False


def test_search_failure_falls_back_cleanly(sandbox):
    res = scout.find(REQ, sk.load(), llm.CostMeter(), [], client=FakeAV(search_error=True))
    assert not res["found"] and "unavailable" in res["search_note"]


def test_external_agent_is_tested_against_the_new_request(sandbox):
    hit = {"address": "agent1qexternal", "name": "isbn-bot",
           "description": "ISBN-13 checker agent replies VALID or INVALID for any ISBN-13 number"}
    av = FakeAV(reply="ok", search=[hit])
    res = scout.find(REQ, sk.load(), llm.CostMeter(), [], client=av)
    assert res["candidate"]["source"] == "agentverse_search"
    assert "from your request" in res["live"]["basis"] and av.invoked[0][0] == "agent1qexternal"
