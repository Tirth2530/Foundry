"""Card Validator: audits EVERY card an agent emitted during testing against the ASI:One
schema, not only the cards a test asked for. Invalid cards are silently dropped by ASI:One,
so this catches failures a human would never see in a log."""
from __future__ import annotations

from .. import cards


def audit(raw: dict) -> list[dict]:
    problems = []
    for case in raw.get("cases") or []:
        if not case:
            continue
        for t in case.get("turns", []):
            for r in t.get("replies", []):
                for md in r.get("metadata", []):
                    if "card_kind" in md or "card_payload" in md:
                        errs = cards.validate(md)
                        if errs:
                            problems.append({"case": case.get("name"), "send": str(t.get("sent", ""))[:80],
                                             "errors": errs[:5]})
    return problems
