"""One client for three model families (GPT, Meta Llama, ASI:One), with cost metering.

Every call is attributed to a role and recorded, so "cost per successful build" is
measured from real token usage rather than estimated.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field

import requests

from . import config


class BudgetExceeded(RuntimeError):
    pass


class PricingUnknown(BudgetExceeded):
    """No configured price for a provider: a paid call cannot be bounded, so it is refused."""


class Cancelled(RuntimeError):
    pass


class LLMError(RuntimeError):
    pass


# ONE lock for every budget operation in the process. Reservation checks walk a chain of budgets
# (job -> evolution run -> project, plus the provider ceiling); doing that under a single lock makes
# check-and-reserve atomic across concurrent calls and cannot deadlock.
_BUDGET_LOCK = threading.RLock()


@dataclass(eq=False)
class CostMeter:
    """A spending budget. `spent` is committed (reconciled) spend, `reserved` is held by in-flight calls.

    A paid call must first RESERVE its worst case (input estimate + the FULL output-token ceiling at the
    configured price) in this meter AND every ancestor; if any level would exceed its cap, nothing is
    reserved and no request is sent. After the call the reservation is released and the actual cost is
    committed. Child spend rolls up to parents."""
    cap_usd: float | None = None
    parent: "CostMeter | None" = None
    name: str = "job"
    job_id: str | None = None
    run_id: str | None = None
    cancel_event: threading.Event | None = None
    calls: list = field(default_factory=list)
    spent: float = 0.0
    reserved: float = 0.0
    overruns: list = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -------------------------------------------------------- hierarchy
    def chain(self) -> list["CostMeter"]:
        out, m = [], self
        while m is not None:
            out.append(m)
            m = m.parent
        return out

    def child(self, cap_usd: float | None = None, name: str = "child", **kw) -> "CostMeter":
        return CostMeter(cap_usd=cap_usd, parent=self, name=name, job_id=kw.get("job_id", self.job_id),
                         run_id=kw.get("run_id", self.run_id), cancel_event=kw.get("cancel_event"))

    def headroom(self) -> float | None:
        """Smallest remaining budget over the chain (None = unlimited)."""
        with _BUDGET_LOCK:
            rooms = [m.cap_usd - m.spent - m.reserved for m in self.chain() if m.cap_usd is not None]
        return None if not rooms else max(0.0, min(rooms))

    # -------------------------------------------------------- cancellation
    def cancelled(self) -> bool:
        return any(m.cancel_event is not None and m.cancel_event.is_set() for m in self.chain())

    def raise_if_cancelled(self) -> None:
        if self.cancelled():
            raise Cancelled("cancelled: no further paid work will start")

    # -------------------------------------------------------- reservation protocol
    def reserve(self, usd: float, provider: str | None = None) -> dict:
        usd = max(0.0, float(usd))
        self.raise_if_cancelled()
        with _BUDGET_LOCK:
            levels = self.chain()
            pm = provider_meter(provider) if provider else None
            if pm is not None and pm not in levels:
                levels = levels + [pm]
            for m in levels:
                if m.cap_usd is not None and m.spent + m.reserved + usd > m.cap_usd + 1e-12:
                    raise BudgetExceeded(
                        f"{m.name} budget ${m.cap_usd:.2f}: ${m.spent:.4f} spent + ${m.reserved:.4f} reserved by "
                        f"in-flight calls; this call needs a ${usd:.4f} reservation (worst case), refused before "
                        "sending")
            for m in levels:
                m.reserved += usd
        return {"usd": usd, "levels": levels}

    def settle(self, reservation: dict, actual_usd: float | None) -> None:
        """Release the reservation and commit the actual cost (None = the call never happened)."""
        with _BUDGET_LOCK:
            for m in reservation["levels"]:
                m.reserved = max(0.0, m.reserved - reservation["usd"])
                if actual_usd:
                    m.spent += actual_usd
            if actual_usd and actual_usd > reservation["usd"] + 1e-9:
                self.overruns.append({"reserved": reservation["usd"], "actual": actual_usd})

    def check(self, upcoming_usd: float = 0.0) -> None:
        """Refuse if `upcoming_usd` would not fit (no reservation is taken)."""
        self.raise_if_cancelled()
        with _BUDGET_LOCK:
            for m in self.chain():
                if m.cap_usd is not None and m.spent + m.reserved + upcoming_usd > m.cap_usd + 1e-12:
                    raise BudgetExceeded(f"{m.name} budget cap ${m.cap_usd:.2f}: ${m.spent:.4f} spent, "
                                         f"${m.reserved:.4f} reserved, next call needs ${upcoming_usd:.4f}")

    def add(self, rec: dict) -> None:
        """Record a call that was already paid (e.g. reported back by a role agent) and roll it up."""
        with _BUDGET_LOCK:
            self.calls.append(rec)
            for m in self.chain()[1:]:
                m.spent += rec.get("usd") or 0.0
            self.spent += rec.get("usd") or 0.0

    # -------------------------------------------------------- reporting
    @property
    def total_usd(self) -> float:
        return round(self.spent, 6)

    def tokens(self) -> dict:
        return {"in": sum(c["tokens_in"] for c in self.calls), "out": sum(c["tokens_out"] for c in self.calls)}

    def by_role(self) -> dict:
        out: dict = {}
        for c in self.calls:
            r = out.setdefault(c["role"], {"usd": 0.0, "calls": 0, "provider": c["provider"]})
            r["usd"] = round(r["usd"] + c["usd"], 6)
            r["calls"] += 1
        return out

    def summary(self) -> dict:
        def tot(k):
            vals = [c.get(k) for c in self.calls if c.get(k) is not None]
            return round(sum(vals), 6) if vals else None
        return {"total_usd": self.total_usd, "tokens": self.tokens(), "by_role": self.by_role(),
                "cap_usd": self.cap_usd,
                "models": sorted(({"provider": c.get("provider"), "model": c.get("model")} for c in self.calls),
                                 key=lambda d: (str(d["provider"]), str(d["model"]))),
                # three kinds of cost, never blended (see docs/BUDGETS.md)
                "estimated_usd": tot("estimated_usd"), "calculated_usd": tot("calculated_usd"),
                "provider_reported_usd": tot("provider_usd"),
                "provider_reported_calls": sum(1 for c in self.calls if c.get("provider_usd") is not None),
                "approximated_usage_calls": sum(1 for c in self.calls if c.get("usage_source") == "approximated"),
                "reservation_overruns": list(self.overruns)}


# ---------------------------------------------------------------- process-wide ceilings
_PROJECT: CostMeter | None = None
_PROVIDERS: dict = {}


def _cap_env(name: str) -> float | None:
    v = config.env(name)
    return float(v) if v not in (None, "", "none") else None


def project_meter() -> CostMeter:
    """FOUNDRY_PROJECT_CAP_USD: total model spend ever allowed for this deployment. Spend already recorded in the
    durable usage ledger counts, so restarting the process does not reset the ceiling."""
    global _PROJECT
    with _BUDGET_LOCK:
        if _PROJECT is None:
            from . import usage
            _PROJECT = CostMeter(cap_usd=_cap_env("FOUNDRY_PROJECT_CAP_USD"), name="project")
            _PROJECT.spent = usage.total_usd()
        return _PROJECT


def provider_meter(provider: str) -> CostMeter | None:
    """FOUNDRY_PROVIDER_CAP_USD_<GPT|META|ASI>: per-provider ceilings, also restart-safe."""
    cap = _cap_env(f"FOUNDRY_PROVIDER_CAP_USD_{provider.upper()}")
    if cap is None:
        return None
    with _BUDGET_LOCK:
        if provider not in _PROVIDERS:
            from . import usage
            m = CostMeter(cap_usd=cap, name=f"provider {provider}")
            m.spent = usage.total_usd(provider=provider)
            _PROVIDERS[provider] = m
        return _PROVIDERS[provider]


def reset_process_budgets() -> None:
    global _PROJECT
    with _BUDGET_LOCK:
        _PROJECT = None
        _PROVIDERS.clear()


def new_meter(cap_usd: float | None = None, *, parent: CostMeter | None = None, name: str = "job",
              job_id: str | None = None, run_id: str | None = None, cancel_event=None,
              detached: bool = False) -> CostMeter:
    """A budget whose ancestors always end at the project ceiling (unless `detached`, used only by role agents
    whose spend is committed by the requesting job instead, so it is never counted twice)."""
    root = None if detached else (parent or project_meter())
    return CostMeter(cap_usd=cap_usd, parent=root, name=name, job_id=job_id or (parent.job_id if parent else None),
                     run_id=run_id or (parent.run_id if parent else None), cancel_event=cancel_event)


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


def estimate_input_tokens(messages: list[dict]) -> int:
    """Conservative: 1 token per 3 characters plus per-message overhead (real tokenisers average ~4)."""
    return int(sum(len(str(m.get("content", ""))) for m in messages) / 3) + 16 * len(messages) + 16


def worst_case_usd(provider_cfg: dict, messages: list[dict], max_tokens: int) -> float:
    pin, pout = provider_cfg.get("price_in"), provider_cfg.get("price_out")
    if pin is None or pout is None:
        raise PricingUnknown(f"no price configured for model {provider_cfg.get('model')!r}: set its *_PRICE_IN / "
                             "*_PRICE_OUT so spend can be bounded before calling it")
    return estimate_input_tokens(messages) / 1e6 * pin + max_tokens / 1e6 * pout


def _provider_cost(usage: dict) -> float | None:
    """Provider-REPORTED cost if the API returns one (OpenRouter-style `cost` / `total_cost`). Never assumed."""
    for k in ("cost", "total_cost"):
        v = usage.get(k)
        if isinstance(v, (int, float)) and v >= 0:
            return float(v)
    return None


def chat(messages: list[dict], *, provider: str, role: str, meter: CostMeter | None = None,
         temperature: float | None = 0.2, max_tokens: int = 6000, retries: int = 3) -> dict:
    """Call a provider. Returns {text, provider, model, fell_back, usd, tokens_in, tokens_out, ...}.

    Spend control: the worst case (estimated input + the FULL max_tokens output at the configured price) is
    reserved in the meter chain before any request is sent; the reservation is replaced by the actual cost
    afterwards. Unknown pricing and cancellation both refuse the call before it is made."""
    used, fell_back = resolve(provider)
    p = config.PROVIDERS[used]
    estimate = worst_case_usd(p, messages, max_tokens)
    if meter is None:
        meter = new_meter(None, name="unattributed")
    reservation = meter.reserve(estimate, provider=used)
    actual = None
    try:
        rec, text = _send(messages, used, p, role, temperature, max_tokens, retries, fell_back)
        rec["estimated_usd"] = round(estimate, 6)
        if rec["usage_source"] == "approximated":
            # the provider did not report usage: charge the full reservation rather than guess low
            rec["usd"] = round(max(rec["calculated_usd"], estimate), 6)
        actual = rec["usd"]
        rec.update(call_id=uuid.uuid4().hex, job_id=meter.job_id, run_id=meter.run_id)
        meter.calls.append(rec)
        try:
            from . import usage
            usage.record({**rec, "cost_source": "provider_reported" if rec["provider_usd"] is not None else
                          ("estimated" if rec["usage_source"] == "approximated" else "calculated")})
        except Exception as e:  # noqa: BLE001 - accounting must never be silently lost
            rec["usage_record_error"] = f"{type(e).__name__}: {e}"
        return {"text": text, **rec}
    finally:
        meter.settle(reservation, actual)


def _send(messages, used, p, role, temperature, max_tokens, retries, fell_back):
    body: dict = {"model": p["model"], "messages": messages}
    if temperature is not None and p["supports_temperature"]:
        body["temperature"] = temperature
    if used == "gpt":
        body["max_completion_tokens"] = max_tokens
    else:
        body["max_tokens"] = max_tokens
    last_err = None
    t0 = time.time()
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
            source = "provider"
            if not (tin or tout):  # provider omitted usage: approximate, and say so
                tin = sum(len(str(m.get("content", ""))) for m in messages) // 4
                tout = len(text) // 4
                source = "approximated"
            calc = tin / 1e6 * p["price_in"] + tout / 1e6 * p["price_out"]
            prov = _provider_cost(usage)
            rec = {"role": role, "provider": used, "model": data.get("model") or p["model"], "fell_back": fell_back,
                   "tokens_in": tin, "tokens_out": tout, "usage_source": source,
                   "calculated_usd": round(calc, 6), "provider_usd": prov,
                   "usd": round(prov if prov is not None else calc, 6),
                   "latency_ms": int(1000 * (time.time() - t0)), "t": time.time()}
            return rec, text
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
