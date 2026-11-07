"""P0.2 regression tests: publication requires real discoverability, failures clean up and say so.

The Agentverse HTTP layer is faked (FakeDeploy); the publisher, teardown, confirm-absent and discovery
logic are the real code. These prove control flow, NOT a live deployment (see `foundry gate1`)."""
import json

import pytest

from foundry import config, ledger, pipeline, skills as sk
from foundry.roles import publisher, scout
from tests.test_security import FakeDeploy, _two_agent_job, _verified_job

CASE = {"name": "x", "turns": [{"send": "abc", "checks": [{"type": "text_contains", "value": "cba"}]}]}


def test_deployed_and_indexed_is_discoverable(sandbox):
    out = publisher.publish(_verified_job(sandbox), CASE, client=FakeDeploy(reply="cba"), discovery_wait_s=0)
    assert out["publication_status"] == "DISCOVERABLE" and out["published"] is True
    assert out["checks"] == {"deployed": "PASS", "running": "PASS", "profile": "PASS", "acp_ack": "PASS",
                             "behaviour": "PASS", "discoverable": "PASS"}


def test_undiscoverable_agent_is_not_labelled_published(sandbox):
    """Answers correctly at its address, but search does not return it: DEPLOYED, not published."""
    fake = FakeDeploy(reply="cba", indexed=False)
    res = _verified_job(sandbox)
    out = publisher.publish(res, CASE, client=fake, discovery_wait_s=0)
    assert out["publication_status"] == "DEPLOYED" and out["published"] is False
    assert out["checks"]["behaviour"] == "PASS" and out["checks"]["discoverable"] == "UNVERIFIED"
    assert out["released"] is True and not fake.deleted  # behaviour verified: deployment kept, honestly labelled
    fake.indexed = True                                    # index catches up later
    again = publisher.recheck(res["job_id"], client=fake)
    assert again["publication_status"] == "DISCOVERABLE"
    assert ledger.read("publish_recheck")[-1]["publication_status"] == "DISCOVERABLE"


def test_failed_profile_update_blocks_release_and_cleans_up(sandbox):
    fake = FakeDeploy(reply="cba", profile_ok=False)
    out = publisher.publish(_verified_job(sandbox), CASE, client=fake, discovery_wait_s=0)
    assert out["publication_status"] == "FAILED" and out["checks"]["profile"] == "FAIL"
    assert out["cleanup_confirmed"] is True and not fake.live


def test_ack_without_correct_behaviour_is_not_published(sandbox):
    fake = FakeDeploy(reply="something else")
    out = publisher.publish(_verified_job(sandbox), CASE, client=fake, discovery_wait_s=0)
    assert out["checks"]["acp_ack"] == "PASS" and out["checks"]["behaviour"] == "FAIL"
    assert out["publication_status"] == "FAILED" and not fake.live
    assert scout.catalog() == []


def test_partial_deployment_cleanup_that_did_not_happen_is_reported(sandbox):
    """delete() returned OK but the agent is still there: cleanup must be reported UNCONFIRMED."""
    fake = FakeDeploy(reply="x", fail_on_upload=2, delete_silently_fails=True)
    out = publisher.publish(_two_agent_job(sandbox), None, client=fake, discovery_wait_s=0)
    assert out["publication_status"] == "FAILED"
    assert out["cleanup_confirmed"] is False and sorted(out["cleanup_unconfirmed"]) == sorted(out["agents"].values())
    assert json.loads((config.RUNS / out["job_id"] / "publish_progress.json").read_text())["stage"] == \
        "cleanup_unconfirmed"


def test_retry_never_deploys_a_duplicate(sandbox):
    res = _verified_job(sandbox)
    fake = FakeDeploy(reply="cba")
    publisher.publish(res, CASE, client=fake, discovery_wait_s=0)
    with pytest.raises(publisher.PublishConflict):
        publisher.publish(res, CASE, client=fake, discovery_wait_s=0)
    assert fake.n == 1


def test_interrupted_publish_must_be_recovered_before_retry(sandbox):
    res = _verified_job(sandbox)
    publisher._progress(res["job_id"], stage="deploying", addresses={"main": "agent1qhalfdone"})
    with pytest.raises(publisher.PublishConflict, match="recover"):
        publisher.publish(res, CASE, client=FakeDeploy(reply="cba"), discovery_wait_s=0)


def test_deploying_code_that_contains_a_factory_credential_is_refused(sandbox, monkeypatch):
    monkeypatch.setattr(config, "AGENTVERSE_API_KEY", "av-very-secret-key-123")
    res = _verified_job(sandbox)
    p = config.RUNS / res["job_id"] / "final" / "build.json"
    b = json.loads(p.read_text())
    b["agents"][0]["code"] += "\nKEY = 'av-very-secret-key-123'\n"
    p.write_text(json.dumps(b))
    fake = FakeDeploy(reply="cba")
    with pytest.raises(ValueError, match="credentials"):
        publisher.publish(res, CASE, client=fake, discovery_wait_s=0)
    assert fake.n == 0


def test_deployed_code_receives_only_peers_never_factory_keys(sandbox, monkeypatch):
    monkeypatch.setattr(config, "AGENTVERSE_API_KEY", "av-very-secret-key-123")
    fake = FakeDeploy(reply="cba")
    publisher.publish(_two_agent_job(sandbox), CASE, client=fake, discovery_wait_s=0)
    for code in fake.uploaded.values():
        assert code.startswith("PEERS = ") and "av-very-secret-key-123" not in code and "test" != code
