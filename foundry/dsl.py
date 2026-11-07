"""Test-case language shared by hidden benchmark tests, the Spec Examiner's independent
tests, and the Adversarial Tester. Evaluation is pure: it reads a recorded transcript.

case  = {"name": str, "turns": [{"send": str, "checks": [check, ...]}]}
check types:
  ack                                   reply acknowledged the message (ChatAcknowledgement)
  text_contains     value | any_of, ci  reply text contains value (or any of)
  text_not_contains value, ci
  text_regex        pattern             re.search, case-insensitive
  text_number       value, tol          some number in the reply is within tol of value
  card              kind?, min_items?, fields?, contains?   a VALID card is present
  no_card                                no card in reply
  end_session                            reply carried EndSessionContent
Every turn implicitly requires at least one reply ChatMessage.
"""
from __future__ import annotations

import re

from . import cards

CHECK_TYPES = {"ack", "text_contains", "text_not_contains", "text_regex", "text_number",
               "card", "no_card", "end_session"}

# Failure codes the Failure Miner clusters on.
NO_REPLY, NO_ACK, TEXT, CARD_MISSING, CARD_INVALID, CARD_WRONG, NO_END, LOAD, STATIC, TIMEOUT = (
    "NO_REPLY", "NO_ACK", "TEXT_MISMATCH", "CARD_MISSING", "CARD_INVALID", "CARD_WRONG",
    "END_SESSION_MISSING", "LOAD_ERROR", "STATIC_VIOLATION", "HARNESS_TIMEOUT")
TOPOLOGY = "TOPOLOGY"


def validate_case(case) -> list[str]:
    errs = []
    if not isinstance(case, dict) or not isinstance(case.get("turns"), list) or not case["turns"]:
        return ["case needs a non-empty 'turns' list"]
    for i, t in enumerate(case["turns"]):
        if not isinstance(t, dict) or not isinstance(t.get("send"), str):
            errs.append(f"turn {i} needs a string 'send'")
            continue
        for c in t.get("checks", []):
            if not isinstance(c, dict) or c.get("type") not in CHECK_TYPES:
                errs.append(f"turn {i} has unknown check {c!r}")
            elif c["type"] in ("text_contains", "text_not_contains") and not (c.get("value") or c.get("any_of")):
                errs.append(f"turn {i} {c['type']} needs value/any_of")
            elif c["type"] == "text_regex":
                try:
                    re.compile(c.get("pattern", ""))
                except re.error as e:
                    errs.append(f"turn {i} bad regex: {e}")
            elif c["type"] == "text_number" and not isinstance(c.get("value"), (int, float)):
                errs.append(f"turn {i} text_number needs numeric value")
    return errs


def _text(turn_rec: dict) -> str:
    return "\n".join(t for r in turn_rec.get("replies", []) for t in r.get("texts", []))


def _cards(turn_rec: dict) -> list[dict]:
    out = []
    for r in turn_rec.get("replies", []):
        for md in r.get("metadata", []):
            if isinstance(md, dict) and ("card_kind" in md or "card_payload" in md):
                out.append(md)
    return out


NUM = re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?")


def evaluate_turn(turn: dict, rec: dict) -> list[dict]:
    """Return [{type, ok, code, detail}] for one turn."""
    results = []
    if rec.get("error"):
        return [{"type": "reply", "ok": False, "code": TIMEOUT, "detail": rec["error"]}]
    if not rec.get("replies"):
        results.append({"type": "reply", "ok": False, "code": NO_REPLY,
                        "detail": f"no ChatMessage reply to {turn['send'][:80]!r}"})
        for c in turn.get("checks", []):
            if c["type"] == "ack":
                results.append({"type": "ack", "ok": bool(rec.get("acked")), "code": NO_ACK,
                                "detail": "" if rec.get("acked") else "no ChatAcknowledgement"})
        return results
    text = _text(rec)
    low = text.lower()
    found_cards = _cards(rec)
    for c in turn.get("checks", []):
        t = c["type"]
        ci = c.get("ci", True)
        hay = low if ci else text
        if t == "ack":
            ok = bool(rec.get("acked"))
            results.append({"type": t, "ok": ok, "code": NO_ACK, "detail": "" if ok else "no ChatAcknowledgement sent"})
        elif t in ("text_contains", "text_not_contains"):
            opts = c.get("any_of") or [c["value"]]
            hit = any((o.lower() if ci else o) in hay for o in opts)
            ok = hit if t == "text_contains" else not hit
            results.append({"type": t, "ok": ok, "code": TEXT,
                            "detail": "" if ok else f"{t} {opts!r}; reply was {text[:200]!r}"})
        elif t == "text_regex":
            ok = re.search(c["pattern"], text, re.I | re.S) is not None
            results.append({"type": t, "ok": ok, "code": TEXT,
                            "detail": "" if ok else f"regex {c['pattern']!r} not found in {text[:200]!r}"})
        elif t == "text_number":
            nums = [float(n.replace(",", "")) for n in NUM.findall(text)]
            tol = float(c.get("tol", 1e-6))
            ok = any(abs(n - float(c["value"])) <= tol for n in nums)
            results.append({"type": t, "ok": ok, "code": TEXT,
                            "detail": "" if ok else f"expected number {c['value']} (±{tol}); found {nums[:6]}"})
        elif t == "card":
            results.append(_check_card(c, found_cards))
        elif t == "no_card":
            ok = not found_cards
            results.append({"type": t, "ok": ok, "code": CARD_WRONG, "detail": "" if ok else "unexpected card"})
        elif t == "end_session":
            ok = any(r.get("end_session") for r in rec.get("replies", []))
            results.append({"type": t, "ok": ok, "code": NO_END, "detail": "" if ok else "no EndSessionContent"})
    return results


def _check_card(c: dict, found: list[dict]) -> dict:
    if not found:
        return {"type": "card", "ok": False, "code": CARD_MISSING,
                "detail": f"expected a {c.get('kind', 'any')} card (MetadataContent) but none was sent"}
    problems = []
    for md in found:
        errs = cards.validate(md)
        if errs:
            problems.append("; ".join(errs[:4]))
            continue
        d = cards.describe(md)
        if c.get("kind") and d["kind"] != c["kind"]:
            problems.append(f"card_kind is {d['kind']!r}, expected {c['kind']!r}")
            continue
        if c.get("min_items") and d["items"] < c["min_items"]:
            problems.append(f"carousel has {d['items']} items, expected >= {c['min_items']}")
            continue
        missing = [f for f in c.get("fields", []) if f not in d["fields"]]
        if missing:
            problems.append(f"form is missing fields {missing} (has {d['fields']})")
            continue
        if c.get("contains") and c["contains"].lower() not in d["text"].lower():
            problems.append(f"card does not mention {c['contains']!r}")
            continue
        return {"type": "card", "ok": True, "code": "", "detail": ""}
    invalid = any(cards.validate(md) for md in found)
    return {"type": "card", "ok": False, "code": CARD_INVALID if invalid else CARD_WRONG,
            "detail": " | ".join(problems)[:600]}


def evaluate_case(case: dict, case_rec: dict) -> dict:
    turns_out = []
    ok_all = True
    for i, turn in enumerate(case["turns"]):
        recs = case_rec.get("turns", [])
        rec = recs[i] if i < len(recs) else {"error": "turn not executed"}
        res = evaluate_turn(turn, rec)
        ok = all(r["ok"] for r in res)
        ok_all &= ok
        turns_out.append({"send": turn["send"], "ok": ok, "checks": res,
                          "reply_text": _text(rec)[:500], "cards": len(_cards(rec))})
    return {"name": case.get("name", "case"), "passed": ok_all, "turns": turns_out}


def failures(case_results: list[dict]) -> list[dict]:
    out = []
    for cr in case_results:
        for t in cr["turns"]:
            for c in t["checks"]:
                if not c["ok"]:
                    out.append({"case": cr["name"], "send": t["send"][:120], "code": c["code"],
                                "detail": c["detail"]})
    return out
