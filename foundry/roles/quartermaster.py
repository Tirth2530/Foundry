"""Quartermaster (financial agent): quotes a build from MEASURED history, sets a hard spend
cap, prices the job, and reconciles actual vs quoted spend afterwards."""
from __future__ import annotations

import math

from .. import config, ledger


def _history(version: str) -> list[dict]:
    rows = [r for r in ledger.read("build") if r.get("usd") is not None and r.get("status") != "error"]
    same = [r for r in rows if r.get("skills_version") == version]
    return (same or rows)[-30:]


def quote(request: str, skills) -> dict:
    hist = _history(skills.version)
    if hist:
        usd = [r["usd"] for r in hist]
        est = sum(usd) / len(usd)
        p90 = sorted(usd)[min(len(usd) - 1, math.ceil(0.9 * len(usd)) - 1)]
        basis = f"mean of {len(hist)} measured builds"
    else:
        b = config.PROVIDERS["gpt"]
        per_attempt = (skills.prompt_tokens() + 3000) / 1e6 * b["price_in"] + 4000 / 1e6 * b["price_out"]
        est = per_attempt * 1.8 + 0.01
        p90 = est * 2
        basis = "token model (no history yet)"
    multi = 1.0 + 0.4 * max(0, request.lower().count("agent") - 1) if "agents" in request.lower() else 1.0
    est, p90 = est * multi, p90 * multi
    cap = round(max(p90 * 1.5, est * 2, 0.10), 2)
    price = max(config.MIN_CHARGE_USD, math.ceil(est * config.MARGIN * 100) / 100)
    return {"estimate_usd": round(est, 4), "p90_usd": round(p90, 4), "cap_usd": cap, "price_usd": round(price, 2),
            "hosting": "Agentverse hosted (not metered)", "test_compute": "local sandbox (not metered)",
            "metered": "model tokens only",
            "basis": basis, "skills_version": skills.version}


def reconcile(q: dict, actual_usd: float) -> dict:
    return {"quoted_usd": q["estimate_usd"], "actual_usd": round(actual_usd, 4), "cap_usd": q["cap_usd"],
            "within_cap": actual_usd <= q["cap_usd"],
            "delta_pct": round(100 * (actual_usd - q["estimate_usd"]) / q["estimate_usd"], 1) if q["estimate_usd"] else None}
