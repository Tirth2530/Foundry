"""Scout: Agentverse search finds candidates; only live tests decide what is trusted."""
import json

from foundry import config, ledger, llm, skills as sk
from foundry.agentverse import AgentverseError
from foundry.roles import scout

REQ = "ISBN-13 checker agent that replies VALID or INVALID for an ISBN-13 number"


class FakeAV:
    """Stands in for Agentverse: search + one relay that calls live agents."""

    def __init__(self, replies=None, hits=None, search_error=False):
        self.replies, self.hits, self.err, self.probes, self.queries = replies or {}, hits or [], search_error, [], []

    def search_agents(self, text, limit=15, semantic=True):
        self.queries.append((text, semantic))
        if self.err:
            raise AgentverseError("search API not production ready")
        return self.hits

    def invoke_many(self, probes, wait_s=180):
        self.probes += probes
        return {p["id"]: {"texts": [self.replies.get(p["target"], "")] if self.replies.get(p["target"]) else [],
                          "cards": [], "acked": True, "latency_s": 7.5, "target": p["target"], "sent": p["send"]}
                for p in probes}


def _seed_catalog():
    jid = "job_abc123"
    d = config.RUNS / jid
    d.mkdir(parents=True)
    (d / "examiner_tests.json").write_text(json.dumps({"cases": [{"name": "v", "turns": [
        {"send": "978-0-306-40615-7", "checks": [{"type": "ack"}, {"type": "text_contains", "value": "VALID"}]}]}]}))
    ledger.append("publish", job_id=jid, agents={"main": "agent1qcatalog"}, entry="agent1qcatalog",
                  request="ISBN-13 checker: reply VALID or INVALID for ISBN-13 numbers", live_passed=True)


HIT_GOOD = {"address": "agent1qgood", "name": "isbn-bot", "status": "active",
            "protocols": [{"name": "AgentChatProtocol", "digest": "proto:x"}], "recent_success_rate": 0.97,
            "readme": "ISBN-13 checker agent. Send an ISBN-13 number and it replies VALID or INVALID."}
HIT_LIAR = {"address": "agent1qliar", "name": "isbn-pro", "status": "active", "recent_success_rate": 0.99,
            "readme": "Best ISBN-13 checker agent, replies VALID or INVALID for every ISBN-13 number."}
HIT_DEAD = {"address": "agent1qdead", "name": "isbn-old", "status": "offline", "readme": "ISBN-13 checker"}
HIT_NOCHAT = {"address": "agent1qnochat", "name": "isbn-raw", "status": "active",
              "protocols": [{"name": "SomeOtherProtocol"}], "readme": "ISBN-13 checker agent VALID INVALID"}


def test_catalog_agent_is_retested_with_its_original_test(sandbox):
    _seed_catalog()
    av = FakeAV(replies={"agent1qcatalog": "VALID"})
    res = scout.find(REQ, sk.load(), llm.CostMeter(), [], client=av)
    assert res["found"] and res["candidate"]["source"] == "foundry_catalog"
    assert {"id": "0:need", "target": "agent1qcatalog", "send": "978-0-306-40615-7"} in av.probes


def test_agentverse_search_is_rereanked_by_live_evidence(sandbox):
    av = FakeAV(hits=[HIT_LIAR, HIT_GOOD, HIT_DEAD, HIT_NOCHAT],
                replies={"agent1qliar": "Sorry, I can only tell jokes", "agent1qgood": "VALID"})
    res = scout.find(REQ, sk.load(), llm.CostMeter(), [], client=av)
    assert len(av.queries) >= 6  # several phrasings x semantic + lexical
    addrs = [a["candidate"]["address"] for a in res["audits"]]
    assert "agent1qdead" not in addrs and "agent1qnochat" not in addrs  # offline / non-chat filtered out
    liar = next(a for a in res["audits"] if a["candidate"]["address"] == "agent1qliar")
    assert liar["need"]["passed"] is False and liar["claim"]["passed"] is False  # 99% listing, fails live
    assert res["found"] and res["candidate"]["address"] == "agent1qgood"
    assert {r["address"]: r["need_passed"] for r in ledger.read("scout_audit")} == {"agent1qliar": False,
                                                                                    "agent1qgood": True}


def test_search_outage_falls_back_to_catalog(sandbox):
    _seed_catalog()
    av = FakeAV(search_error=True, replies={"agent1qcatalog": "VALID"})
    res = scout.find(REQ, sk.load(), llm.CostMeter(), [], client=av)
    assert res["search"]["errors"] and res["found"]  # search down, catalog still works


def test_nothing_passes_means_build(sandbox):
    av = FakeAV(hits=[HIT_LIAR], replies={"agent1qliar": "jokes only"})
    res = scout.find(REQ, sk.load(), llm.CostMeter(), [], client=av)
    assert not res["found"] and res["live"]["passed"] is False
