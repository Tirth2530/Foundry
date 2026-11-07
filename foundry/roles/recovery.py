"""Recovery: bounded retries with provider failover. Never retries a blown budget."""
from __future__ import annotations

import time

from .. import config, llm


def call(fn, *, provider: str, attempts: int = 3, events: list | None = None):
    """Run fn(provider) with retries; on repeated provider failure switch model family."""
    tried: list[str] = []
    order = [provider] + [p for p in config.FALLBACK_ORDER if p != provider]
    last = None
    for p in order:
        if not llm.available(p) and p != provider:
            continue
        for a in range(attempts):
            try:
                return fn(p)
            except llm.BudgetExceeded:
                raise
            except (llm.LLMError, ValueError, KeyError) as e:
                last = e
                tried.append(f"{p}#{a + 1}: {str(e)[:160]}")
                if events is not None:
                    events.append({"recovery": "retry", "provider": p, "attempt": a + 1, "error": str(e)[:200]})
                time.sleep(0.5 * (a + 1))
        if events is not None:
            events.append({"recovery": "failover", "from": p})
    raise llm.LLMError(f"all providers failed: {tried[-3:]} last={last}")
