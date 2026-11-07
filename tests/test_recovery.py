"""Durable jobs: validated transitions and crash recovery that never repeats paid or side-effecting work."""
import json

import pytest

from foundry import config, jobs, ledger
from foundry.roles import publisher
from tests.test_security import FakeDeploy, _verified_job


def test_illegal_transitions_are_refused():
    jobs.check_transition("quoted", "building")
    jobs.check_transition("verified", "publishing")
    for old, new in (("quoted", "verified"), ("cancelled", "building"), ("unverified", "publishing"),
                     ("awaiting_payment", "building"), ("verified", "cancelled")):
        with pytest.raises(jobs.InvalidTransition):
            jobs.check_transition(old, new)


def test_crashed_build_is_marked_interrupted_not_resumed(sandbox):
    store = {"job_aaaaaa": {"job_id": "job_aaaaaa", "status": "building"},
             "job_bbbbbb": {"job_id": "job_bbbbbb", "status": "verified"}}
    seen = []

    def persist(job, **kw):
        jobs.check_transition(job["status"], kw["status"])
        job.update(kw)
        seen.append(job["job_id"])
    out = jobs.recover(store, persist)
    assert store["job_aaaaaa"]["status"] == "interrupted" and store["job_bbbbbb"]["status"] == "verified"
    assert seen == ["job_aaaaaa"] and out[0]["action"] == "marked interrupted"
    assert ledger.read("job_interrupted")[-1]["resumed"] is False


def test_crash_mid_deploy_is_torn_down_on_restart(sandbox):
    res = _verified_job(sandbox)
    fake = FakeDeploy(reply="cba")
    a1, a2 = fake.create("x"), fake.create("y")      # two agents were created, then the process died
    publisher._progress(res["job_id"], stage="deploying", addresses={"main": a1, "peer": a2})
    store = {res["job_id"]: {"job_id": res["job_id"], "status": "publishing"}}
    jobs.recover(store, lambda job, **kw: job.update(kw), client_factory=lambda: fake)
    assert store[res["job_id"]]["status"] == "verified" and not fake.live
    prog = json.loads((config.RUNS / res["job_id"] / "publish_progress.json").read_text())
    assert prog["stage"] == "rolled_back"
    # now a normal retry is allowed again, and makes exactly one new deployment
    case = {"name": "x", "turns": [{"send": "abc", "checks": [{"type": "text_contains", "value": "cba"}]}]}
    out = publisher.publish(res, case, client=fake, discovery_wait_s=0)
    assert out["released"] and len(fake.live) == 1


def test_unconfirmed_recovery_is_left_visible(sandbox):
    res = _verified_job(sandbox)
    fake = FakeDeploy(reply="cba", delete_silently_fails=True)
    a1 = fake.create("x")
    publisher._progress(res["job_id"], stage="verifying", addresses={"main": a1})
    store = {res["job_id"]: {"job_id": res["job_id"], "status": "publishing"}}
    jobs.recover(store, lambda job, **kw: job.update(kw), client_factory=lambda: fake)
    assert store[res["job_id"]]["status"] == "interrupted"
    assert store[res["job_id"]]["unconfirmed_cleanup"] == [a1]
