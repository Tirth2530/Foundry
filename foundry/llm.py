"""One client for three model families (GPT, Meta Llama, ASI:One), with cost metering.

Every call is attributed to a role and recorded, so "cost per successful build" is
measured from real token usage rather than estimated.
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field

import requests

from . import config


class BudgetExceeded(RuntimeError):
    pass


class LLMError(RuntimeError):
    pass


@dataclass
class CostMeter:
    cap_usd: float | None = None
    calls: list = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def total_usd(self) -> float:
        return round(sum(c["usd"] for c in self.calls), 6)

    def tokens(self) -> dict:
        return {
            "in": sum(c["tokens_in"] for c in self.calls),
            "out": sum(c["tokens_out"] for c in self.calls),
        }

    def by_role(self) -> dict:
        out: dict = {}
        for c in self.calls:
            r = out.setdefault(c["role"], {"usd": 0.0, "calls": 0, "provider": c["provider"]})
            r["usd"] = round(r["usd"] + c["usd"], 6)
            r["calls"] += 1
        return out

    def check(self) -> None:
        if self.cap_usd is not None and self.total_usd >= self.cap_usd:
            raise BudgetExceeded(f"budget cap ${self.cap_usd:.2f} reached (${self.total_usd:.4f} spent)")

    def add(self, rec: dict) -> None:
        with self._lock:
            self.calls.append(rec)

    def summary(self) -> dict:
        return {"total_usd": self.total_usd, "tokens": self.tokens(), "by_role": self.by_role(),
                "cap_usd": self.cap_usd}


def available(provider: str) -> bool:
    return bool(config.PROVIDERS.get(provider, {}).get("api_key"))


def resolve(provider: str) -> tuple[str, bool]:
    """Return (provider actually used, whether it fell back)."""
    if available(provider):
        return provider, False
    for p in config.FALLBACK_ORDER:
        if available(p):
            return p, True
    raise LLMError("No model API keys configured. Set OPENAI_API_KEY, LLAMA_API_KEY or ASI_ONE_API_KEY.")


def chat(messages: list[dict], *, provider: str, role: str, meter: CostMeter | None = None,
         temperature: float | None = 0.2, max_tokens: int = 6000, retries: int = 3) -> dict:
    """Call a provider. Returns {text, provider, model, fell_back, usd, tokens_in, tokens_out}."""
    if meter:
        meter.check()
    used, fell_back = resolve(provider)
    p = config.PROVIDERS[used]
    body: dict = {"model": p["model"], "messages": messages}
    if temperature is not None and p["supports_temperature"]:
        body["temperature"] = temperature
    if used == "gpt":
        body["max_completion_tokens"] = max_tokens
    else:
        body["max_tokens"] = max_tokens

    last_err = None
    for attempt in range(retries):
        try:
            r = requests.post(
                f"{p['base_url'].rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {p['api_key']}", "Content-Type": "application/json"},
                json=body, timeout=180,
            )
            if r.status_code == 400 and "temperature" in r.text and "temperature" in body:
                body.pop("temperature")  # some models reject custom temperature
                continue
            if r.status_code == 400 and "max_completion_tokens" in r.text:
                body["max_tokens"] = body.pop("max_completion_tokens")
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                last_err = f"HTTP {r.status_code}: {r.text[:300]}"
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code >= 300:
                raise LLMError(f"{used} HTTP {r.status_code}: {r.text[:500]}")
            data = r.json()
            msg = data["choices"][0]["message"]
            text = msg.get("content") or ""
            usage = data.get("usage") or {}
            tin = int(usage.get("prompt_tokens") or 0)
            tout = int(usage.get("completion_tokens") or 0)
            if not tin:  # rough fallback if a provider omits usage
                tin = sum(len(m.get("content", "")) for m in messages) // 4
                tout = len(text) // 4
            usd = tin / 1e6 * p["price_in"] + tout / 1e6 * p["price_out"]
            rec = {"role": role, "provider": used, "model": p["model"], "fell_back": fell_back,
                   "tokens_in": tin, "tokens_out": tout, "usd": round(usd, 6), "t": time.time()}
            if meter:
                meter.add(rec)
            return {"text": text, **rec}
        except requests.RequestException as e:
            last_err = str(e)
            time.sleep(2 * (attempt + 1))
    raise LLMError(f"{used} failed after {retries} attempts: {last_err}")


# ---------------------------------------------------------------- output parsing helpers

def extract_json(text: str):
    """Pull the first JSON object/array out of a model reply (handles ```json fences)."""
    fence = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.S)
    candidates = [fence.group(1)] if fence else []
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            candidates.append(text[start:end + 1])
    for c in candidates:
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            continue
    raise LLMError(f"model did not return valid JSON: {text[:300]}")


CODE_BLOCK = re.compile(r"```python[ \t]+name=([A-Za-z_][A-Za-z0-9_]*)([^\n]*)\n(.*?)```", re.S)


def extract_agents(text: str) -> dict:
    """Parse builder output: one ```python name=<agent> [entry] block per agent."""
    agents, entry = [], None
    for m in CODE_BLOCK.finditer(text):
        name, flags, code = m.group(1), m.group(2), m.group(3)
        agents.append({"name": name, "code": code.strip() + "\n"})
        if "entry" in flags:
            entry = name
    if not agents:  # tolerate a single plain ```python block
        plain = re.search(r"```python\s*\n(.*?)```", text, re.S)
        if plain:
            agents = [{"name": "main", "code": plain.group(1).strip() + "\n"}]
    if not agents:
        raise LLMError("builder returned no code blocks")
    return {"agents": agents, "entry": entry or agents[0]["name"]}
