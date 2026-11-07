"""Job lifecycle: validated state transitions and bounded recovery after a crash or restart.

Pattern adapted from Dapta's JobStore (backend/dapta/jobs.py, MIT, Copyright (c) 2026 Aditya Venkat; see
THIRD_PARTY_NOTICES.md): an explicit transition table, an `interrupted` state for work whose process died,
"cancellation requested" kept distinct from "cleanup completed", and recovery that never re-runs a paid or
side-effecting step on its own.

FOUNDRY's job record stays the job.json file the Foreman already persists (no second source of truth); this
module only decides which status changes are legal and what to do with jobs found mid-flight at startup.
"""
from __future__ import annotations

import json

from . import config, ledger

TERMINAL_BUILD = {"verified", "unverified", "budget_exceeded", "error", "cancelled"}
ALLOWED = {
    None: {"quoted", "scouted", "reuse_offer"},
    "scouted": {"quoted", "reused", "cancelled"},
    "reuse_offer": {"quoted", "reused", "cancelled"},
    "quoted": {"awaiting_payment", "building", "cancelled", "quoted"},
    "awaiting_payment": {"quoted", "cancelled"},
    "building": TERMINAL_BUILD | {"cancelling", "interrupted"},
    "cancelling": TERMINAL_BUILD | {"interrupted"},
    "verified": {"publishing"},
    "publishing": {"verified", "interrupted"},
    "interrupted": {"verified", "cancelled"},
    "unverified": set(), "budget_exceeded": set(), "error": set(), "cancelled": set(), "reused": set(),
}


class InvalidTransition(ValueError):
    pass


def check_transition(old: str | None, new: str) -> None:
    if old == new:
        return
    if new not in ALLOWED.get(old, set()):
        raise InvalidTransition(f"job cannot go from {old!r} to {new!r}")


def recover(jobs: dict, persist, client_factory=None) -> list[dict]:
    """Called once at Foreman startup with every persisted job.

    building / cancelling -> interrupted   the build process died; it is NOT resumed automatically (a resumed build
                                           could repeat paid model calls). The user can request it again.
    publishing            -> verified      after tearing down every agent the interrupted publish had created
                                           (addresses come from publish_progress.json). If the teardown cannot be
                                           CONFIRMED the job is left 'interrupted' and says which addresses remain.
    """
    actions = []
    for job in jobs.values():
        st = job.get("status")
        if st in ("building", "cancelling"):
            persist(job, status="interrupted", interrupted_from=st)
            ledger.append("job_interrupted", job_id=job["job_id"], from_status=st, resumed=False)
            actions.append({"job_id": job["job_id"], "action": "marked interrupted", "from": st})
        elif st == "publishing":
            res = recover_publish(job["job_id"], client_factory)
            if res.get("cleanup_confirmed") is not False:
                persist(job, status="verified", last_publish_attempt={"recovered": res})
            else:
                persist(job, status="interrupted", interrupted_from=st, unconfirmed_cleanup=res.get("unconfirmed"))
            actions.append({"job_id": job["job_id"], "action": "publish recovery", **res})
    return actions


def recover_publish(job_id: str, client_factory=None) -> dict:
    """Tear down a deployment that was interrupted before it was kept or rolled back."""
    p = config.RUNS / job_id / "publish_progress.json"
    if not p.exists():
        return {"cleanup_confirmed": None, "note": "no deployment progress recorded"}
    prog = json.loads(p.read_text())
    if prog.get("stage") not in ("deploying", "verifying"):
        return {"cleanup_confirmed": None, "note": f"publish already {prog.get('stage')}"}
    addrs = list((prog.get("addresses") or {}).values())
    if not addrs:
        prog["stage"] = "rolled_back"
        p.write_text(json.dumps(prog, indent=1))
        return {"cleanup_confirmed": True, "note": "nothing had been created"}
    from . import agentverse
    client = (client_factory or agentverse.Client)()
    report = client.teardown(addrs)
    unconfirmed = [a for a, r in report.items() if not (r.get("delete") == "ok" and r.get("absent") is True)]
    prog.update(stage="rolled_back" if not unconfirmed else "cleanup_unconfirmed", recovery=report)
    p.write_text(json.dumps(prog, indent=1))
    ledger.append("publish_recovered", job_id=job_id, addresses=addrs, unconfirmed=unconfirmed)
    return {"cleanup_confirmed": not unconfirmed, "unconfirmed": unconfirmed, "report": report}


def recover_all_publishes(client_factory=None) -> list[dict]:
    """CLI `foundry recover`: clean up every interrupted publish found on disk."""
    out = []
    for p in sorted(config.RUNS.glob("*/publish_progress.json")):
        out.append({"job_id": p.parent.name, **recover_publish(p.parent.name, client_factory)})
    return out
