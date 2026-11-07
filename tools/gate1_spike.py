"""
FOUNDRY - Gate 1 spike
Proves: FOUNDRY can programmatically CREATE, DEPLOY and INDEPENDENTLY INVOKE
a brand-new ACP-compatible agent on Agentverse, with a behavioral check
(not just "it replied") and a saved evidence file.

How it works (all over the Agentverse REST API, no browser):
  1. Create hosted TARGET agent, upload generated code, start it.
  2. Create hosted RELAY agent that sends the target a ChatMessage on startup.
  3. Poll the relay's logs for the target's reply.
  4. Check behavior: target must return the probe string REVERSED (deterministic oracle).
  5. Write evidence/gate1.json  (observed facts only).

Run:
  export AGENTVERSE_API_KEY=...        # agentverse.ai -> Profile -> API keys
  pip install requests
  python gate1_spike.py

If an endpoint shape differs from what this expects, the script prints the raw
response so you can adjust in one place (the API_* helpers below).
"""

import json
import os
import random
import string
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

try:  # reuse FOUNDRY's .env loader when run inside the repo
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import foundry.config  # noqa: F401  (loads .env)
except Exception:
    pass

BASE = os.getenv("AGENTVERSE_URL", "https://agentverse.ai")
KEY = os.getenv("AGENTVERSE_API_KEY")
if not KEY:
    sys.exit("Set AGENTVERSE_API_KEY first.")
H = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

RUN_ID = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
PROBE = f"foundry-{RUN_ID}-" + "".join(random.choices(string.ascii_letters, k=12))
EXPECTED = f"FOUNDRY-GATE1-OK:{PROBE[::-1]}"

# ---------------------------------------------------------------- generated agent code
# Hosted rules: do NOT create Agent(); `agent` is provided. Only stdlib + uagents(+core).

TARGET_CODE = '''
from datetime import datetime, timezone
from uuid import uuid4
from uagents import Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement, ChatMessage, EndSessionContent, TextContent, chat_protocol_spec,
)

protocol = Protocol(spec=chat_protocol_spec)

@protocol.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(
        timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
    text = " ".join(c.text for c in msg.content if isinstance(c, TextContent))
    reply = "FOUNDRY-GATE1-OK:" + text.strip()[::-1]
    ctx.logger.info("TARGET_RECEIVED " + text)
    await ctx.send(sender, ChatMessage(
        timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
        content=[TextContent(type="text", text=reply), EndSessionContent(type="end-session")]))

@protocol.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass

agent.include(protocol, publish_manifest=True)
'''

RELAY_TEMPLATE = '''
from datetime import datetime, timezone
from uuid import uuid4
from uagents import Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement, ChatMessage, TextContent, chat_protocol_spec,
)

TARGET = "__TARGET__"
PROBE = "__PROBE__"
protocol = Protocol(spec=chat_protocol_spec)

@agent.on_event("startup")
async def go(ctx: Context):
    ctx.logger.info("RELAY_SENDING " + PROBE)
    await ctx.send(TARGET, ChatMessage(
        timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
        content=[TextContent(type="text", text=PROBE)]))

@protocol.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(
        timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
    text = " ".join(c.text for c in msg.content if isinstance(c, TextContent))
    ctx.logger.info("GATE1_RESPONSE " + text)

@protocol.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    ctx.logger.info("GATE1_ACK " + str(msg.acknowledged_msg_id))

agent.include(protocol, publish_manifest=True)
'''

# ---------------------------------------------------------------- API helpers


def _check(r, what):
    if r.status_code >= 300:
        print(f"[{what}] HTTP {r.status_code}: {r.text[:800]}")
        r.raise_for_status()
    try:
        return r.json()
    except ValueError:
        return {}


def api_create(name):
    data = _check(requests.post(f"{BASE}/v1/hosting/agents", headers=H,
                                json={"name": name}, timeout=30), "create")
    addr = data.get("address") or data.get("agent", {}).get("address")
    if not addr:
        sys.exit(f"Could not find address in create response: {data}")
    return addr


def api_upload(addr, code):
    files = [{"language": "python", "name": "agent.py", "value": code}]
    _check(requests.put(f"{BASE}/v1/hosting/agents/{addr}/code", headers=H,
                        json={"code": json.dumps(files)}, timeout=30), "upload")


def api_start(addr):
    _check(requests.post(f"{BASE}/v1/hosting/agents/{addr}/start", headers=H,
                         timeout=30), "start")


def api_logs(addr):
    r = requests.get(f"{BASE}/v1/hosting/agents/{addr}/logs/latest", headers=H, timeout=30)
    if r.status_code >= 300:
        return ""
    return r.text  # search raw text; robust to log schema changes


def wait_for(addr, marker, timeout_s):
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        logs = api_logs(addr)
        if marker in logs:
            return logs
        time.sleep(4)
    return None


def extract(logs, marker):
    i = logs.find(marker)
    if i < 0:
        return None
    tail = logs[i + len(marker):].lstrip()
    for stop in ['"', "\\n", "\n", "}"]:
        j = tail.find(stop)
        if j >= 0:
            tail = tail[:j]
    return tail.strip()


# ---------------------------------------------------------------- run


def main():
    ev = {"gate": 1, "run_id": RUN_ID, "started": datetime.now(timezone.utc).isoformat(),
          "probe": PROBE, "expected": EXPECTED, "steps": []}

    def step(name, **kw):
        kw.update(step=name, t=round(time.time() - t0, 1))
        ev["steps"].append(kw)
        print(f"[{kw['t']:>6}s] {name} {kw if len(kw) > 2 else ''}")

    t0 = time.time()
    target = api_create(f"foundry-target-{RUN_ID}")
    step("target_created", address=target)
    api_upload(target, TARGET_CODE)
    api_start(target)
    step("target_started")

    # give the target time to register on the Almanac so it is resolvable
    print("waiting 45s for target registration...")
    time.sleep(45)

    relay = api_create(f"foundry-relay-{RUN_ID}")
    step("relay_created", address=relay)
    api_upload(relay, RELAY_TEMPLATE.replace("__TARGET__", target).replace("__PROBE__", PROBE))
    api_start(relay)
    step("relay_started")

    logs = wait_for(relay, "GATE1_RESPONSE", timeout_s=180)
    if not logs:
        step("no_response")
        ev["result"] = "FAIL: no response within 180s"
        ev["relay_logs_tail"] = api_logs(relay)[-3000:]
        ev["target_logs_tail"] = api_logs(target)[-3000:]
    else:
        actual = extract(logs, "GATE1_RESPONSE")
        ok = actual == EXPECTED
        step("response", actual=actual, behavioral_check=ok)
        ev["observed"] = {"actual": actual, "behavioral_match": ok}
        ev["result"] = "PASS" if ok else "FAIL: wrong behavior"

    ev["finished"] = datetime.now(timezone.utc).isoformat()
    root = Path(__file__).resolve().parent.parent
    (root / "evidence").mkdir(exist_ok=True)
    out = root / "evidence" / "gate1.json"
    out.write_text(json.dumps(ev, indent=2))
    print(f"\nRESULT: {ev['result']}\nEvidence: {out}")
    print("Note: stop or delete foundry-* test agents in Agentverse afterwards to free slots.")


if __name__ == "__main__":
    main()
