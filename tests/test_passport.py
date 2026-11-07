"""Proof Passport: issued from real build evidence, validated deterministically, and tamper-evident against
the files and the ledger. (Builds here use the scripted model + runner; the passport logic is real.)"""
import json

import pytest

from foundry import config, ledger, passport, pipeline, skills as sk
from tests.test_security import FakeDeploy

CASE = {"name": "x", "turns": [{"send": "abc", "checks": [{"type": "text_contains", "value": "cba"}]}]}


def _build(sandbox):
    res = pipeline.build("an agent that reverses text", sk.load(), budget_usd=1.0)
    assert res["status"] == "verified" and res.get("passport"), res.get("passport_error")
    return res, config.RUNS / res["job_id"] / "passport.json"


def test_verified_build_gets_a_valid_passport(sandbox):
    res, path = _build(sandbox)
    pp = json.loads(path.read_text())
    assert passport.validate(path) == []
    assert pp["verdict"] == "VERIFIED" and pp["tests"]["status"] == "PASS"
    assert pp["examiner"]["sees_code"] is False and pp["examiner"]["certifies_itself"] is False
    assert pp["collaboration"]["status"] == "NOT TESTED"           # single agent, honestly labelled
    assert pp["publication"]["status"] == "NOT PUBLISHED"
    assert {c["kind"] for c in pp["claims"]} == {"OBSERVED", "INFERRED"}
    assert all(c["status"] != "PASS" for c in pp["claims"] if c["kind"] == "INFERRED")
    assert ledger.read("passport_issued")[-1]["passport_id"] == pp["passport_id"]
    assert pp["ledger_anchor"]["hash"] == [r for r in ledger.read("build") if r["job_id"] == res["job_id"]][-1]["hash"]


def test_code_changed_after_verification_invalidates_the_passport(sandbox):
    res, path = _build(sandbox)
    agent = config.RUNS / res["job_id"] / "final" / "agents" / "main.py"
    agent.write_text(agent.read_text() + "\n# sneaky edit\n")
    assert any("code digest" in p for p in passport.validate(path))


def test_edited_passport_or_evidence_is_detected(sandbox):
    res, path = _build(sandbox)
    pp = json.loads(path.read_text())
    pp["tests"]["passed"] = 999
    path.write_text(json.dumps(pp))
    assert any("integrity" in p for p in passport.validate(path))
    res2, path2 = _build(sandbox)
    (config.RUNS / res2["job_id"] / "examiner_tests.json").write_text('{"cases": []}')
    assert any("evidence file changed" in p for p in passport.validate(path2))


def test_inferred_claim_marked_pass_is_rejected_even_with_a_recomputed_digest(sandbox):
    """Someone 'upgrades' an inference to PASS and recomputes the body digest: the rules still catch it."""
    _, path = _build(sandbox)
    pp = json.loads(path.read_text())
    for c in pp["claims"]:
        if c["kind"] == "INFERRED":
            c["status"] = "PASS"
    pp["integrity"]["body_sha256"] = passport.sha256_bytes(passport.canonical(
        {k: v for k, v in pp.items() if k != "integrity"}))
    path.write_text(json.dumps(pp))
    assert any("INFERRED claim" in p for p in passport.validate(path))


def test_verified_multi_agent_without_observed_collaboration_is_inconsistent(sandbox):
    _, path = _build(sandbox)
    pp = json.loads(path.read_text())
    pp["code"]["agent_count"] = 3                     # forged: three agents, collaboration never observed
    pp["integrity"]["body_sha256"] = passport.sha256_bytes(passport.canonical(
        {k: v for k, v in pp.items() if k != "integrity"}))
    path.write_text(json.dumps(pp))
    assert any("collaboration was not observed" in p for p in passport.validate(path))


def test_broken_ledger_chain_invalidates(sandbox):
    _, path = _build(sandbox)
    lines = ledger.PATH.read_text().splitlines()
    first = json.loads(lines[0])
    first["status"] = "tampered"
    lines[0] = json.dumps(first)
    ledger.PATH.write_text("\n".join(lines) + "\n")
    assert any("hash chain is broken" in p for p in passport.validate(path))


def test_passport_is_reissued_after_publication_with_receipts(sandbox):
    res, path = _build(sandbox)
    from foundry.roles import publisher
    publisher.publish(res, CASE, client=FakeDeploy(reply="cba", indexed=False), discovery_wait_s=0)
    pp = json.loads(path.read_text())
    assert passport.validate(path) == []
    assert pp["publication"]["status"] == "DEPLOYED" and pp["publication"]["discoverable"] == "UNVERIFIED"
    assert pp["external_actions"][0]["status"] == "PASS" and pp["external_actions"][0]["kind"] == "OBSERVED"
    assert any("ASI:One user" in u for u in pp["untested_assumptions"])
    rows = dict(passport.summary_rows(pp))
    assert rows["Verdict"] == "VERIFIED" and "DEPLOYED" in rows["Publication"]


def test_passport_verify_cli_exit_codes(sandbox):
    from foundry import __main__ as cli
    _, path = _build(sandbox)

    class A:
        pass
    a = A()
    a.path = str(path)
    cli.cmd_passport_verify(a)                         # valid: returns normally
    path.write_text(path.read_text().replace('"VERIFIED"', '"VERIFIED "', 1))
    with pytest.raises(SystemExit) as e:
        cli.cmd_passport_verify(a)
    assert e.value.code == 1
