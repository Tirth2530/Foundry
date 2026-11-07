"""Central configuration. Reads environment variables (and a local .env file if present).

Nothing here needs third-party packages, so the pure parts of FOUNDRY (tests, validators,
ledger) run anywhere.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BENCH = ROOT / "bench"
SKILLS = ROOT / "skills"
RUNS = ROOT / "runs"
EVIDENCE = ROOT / "evidence"


def _load_dotenv(path: Path) -> None:
    """Tiny .env loader (KEY=VALUE lines). Real environment variables win."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


_load_dotenv(ROOT / ".env")


def env(name: str, default: str | None = None) -> str | None:
    val = os.getenv(name)
    return val if val not in (None, "") else default


# ---------------------------------------------------------------- model providers
# All three speak the OpenAI-compatible /chat/completions API.
PROVIDERS = {
    "gpt": {
        "base_url": env("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "api_key": env("OPENAI_API_KEY"),
        "model": env("OPENAI_MODEL", "gpt-5"),
        # USD per 1M tokens (input, output). Override to match your model's pricing.
        "price_in": float(env("OPENAI_PRICE_IN", "1.25")),
        "price_out": float(env("OPENAI_PRICE_OUT", "10.0")),
        "supports_temperature": env("OPENAI_SUPPORTS_TEMPERATURE", "false") == "true",
        # gpt-5 / o-series: low effort = faster, cheaper, and code output isn't starved by hidden reasoning
        "reasoning_effort": env("OPENAI_REASONING_EFFORT",
                                "low" if env("OPENAI_MODEL", "gpt-5").startswith(("gpt-5", "o")) else "") or None,
    },
    "meta": {
        "base_url": env("LLAMA_BASE_URL", "https://api.meta.ai/v1"),
        "api_key": env("LLAMA_API_KEY"),
        "model": env("LLAMA_MODEL", "muse-spark-1.1"),
        "price_in": float(env("LLAMA_PRICE_IN", "0.27")),
        "price_out": float(env("LLAMA_PRICE_OUT", "0.85")),
        "supports_temperature": True,
    },
    "asi": {
        "base_url": env("ASI_BASE_URL", "https://api.asi1.ai/v1"),
        "api_key": env("ASI_ONE_API_KEY"),
        "model": env("ASI_MODEL", "asi1"),
        "price_in": float(env("ASI_PRICE_IN", "1.0")),
        "price_out": float(env("ASI_PRICE_OUT", "1.0")),
        "supports_temperature": True,
    },
}

# If a role's provider has no key, fall back in this order (and record it in evidence).
FALLBACK_ORDER = ["gpt", "meta", "asi"]

# ---------------------------------------------------------------- Agentverse / payments
AGENTVERSE_URL = env("AGENTVERSE_URL", "https://agentverse.ai")
AGENTVERSE_API_KEY = env("AGENTVERSE_API_KEY")
FOREMAN_SEED = env("FOUNDRY_SEED", "foundry-demo-seed-change-me")
FOREMAN_PORT = int(env("FOUNDRY_PORT", "8000"))
NETWORK_MODE = env("FOUNDRY_NETWORK_MODE", "split")  # split | bureau | direct

STRIPE_SECRET_KEY = env("STRIPE_SECRET_KEY")
STRIPE_PUBLISHABLE_KEY = env("STRIPE_PUBLISHABLE_KEY")
STRIPE_SUCCESS_URL = env("STRIPE_SUCCESS_URL", "https://asi1.ai")
MIN_CHARGE_USD = float(env("FOUNDRY_MIN_CHARGE_USD", "0.50"))  # Stripe minimum
MARGIN = float(env("FOUNDRY_MARGIN", "1.5"))  # price = estimated cost * margin

# ---------------------------------------------------------------- build loop defaults
DEFAULT_BUDGET_USD = float(env("FOUNDRY_BUDGET_USD", "1.50"))
TURN_TIMEOUT_S = float(env("FOUNDRY_TURN_TIMEOUT", "15"))
PARALLEL_BUILDS = int(env("FOUNDRY_PARALLEL", "4"))
