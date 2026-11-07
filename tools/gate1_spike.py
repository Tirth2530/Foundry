"""
FOUNDRY Gate 1: can FOUNDRY create, deploy and INDEPENDENTLY invoke a brand-new
ACP agent on Agentverse, and check its behaviour (not just that it replied)?

  1. create a hosted TARGET agent, upload generated code, start it   (v1 hosting API, v2 fallback)
  2. a separate throwaway RELAY agent sends it a random probe over ACP
  3. the reply must be the probe REVERSED (a deterministic oracle)
  4. evidence/gate1.json records what was observed; both agents are stopped and deleted afterwards

Run from the repo root:  python -m foundry gate1      (needs AGENTVERSE_API_KEY in .env)
"""
import json
import random
import string
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from foundry import agentverse, config  # noqa: E402  (loads .env)

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
    await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
    text = " ".join(c.text for c in msg.content if isinstance(c, TextContent))
    await ctx.send(sender, ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
        content=[TextContent(type="text", text="FOUNDRY-GATE1-OK:" + text.strip()[::-1]),
                 EndSessionContent(type="end-session")]))

@protocol.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass

agent.include(protocol, publish_manifest=True)
'''


def main():
    if not config.AGENTVERSE_API_KEY:
        sys.exit("Set AGENTVERSE_API_KEY in .env first.")
    c = agentverse.Client()
    run_id = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    probe = f"foundry-{run_id}-" + "".join(random.choices(string.ascii_letters, k=12))
    expected = f"FOUNDRY-GATE1-OK:{probe[::-1]}"
    ev = {"gate": 1, "run_id": run_id, "started": datetime.now(timezone.utc).isoformat(),
          "probe": probe, "expected": expected, "steps": []}
    t0 = time.time()

    def step(name, **kw):
        kw.update(step=name, t=round(time.time() - t0, 1))
        ev["steps"].append(kw)
        print(f"[{kw['t']:>6}s] {name} {json.dumps({k: v for k, v in kw.items() if k not in ('step', 't')})}")

    target = None
    try:
        target = c.create(f"foundry-gate1-{run_id}")
        step("target_created", address=target)
        c.upload(target, TARGET_CODE)
        c.start(target)
        step("target_started")
        running = c.wait_running(target, timeout=90)
        step("target_running", running=running, hosting_api=c.api)
        time.sleep(15)  # give the platform a moment to route messages to the new address
        reply = c.invoke(target, probe, wait_s=180)
        actual = (reply.get("texts") or [None])[0]
        ok = actual == expected
        step("independent_invocation", relay=reply.get("relay"), relay_cleanup=reply.get("relay_cleanup"),
             acked=reply.get("acked"),
             latency_s=reply.get("latency_s"), actual=actual, behavioral_match=ok)
        ev["observed"] = {"actual": actual, "behavioral_match": ok, "acked": reply.get("acked")}
        ev["result"] = "PASS" if ok else ("FAIL: no reply" if actual is None else "FAIL: wrong behaviour")
        if not ok:
            ev["target_logs_tail"] = c.logs(target)[-3000:]
    except agentverse.AgentverseError as e:
        ev["result"] = f"FAIL: {e}"
        step("api_error", error=str(e)[:400])
    finally:
        if target:
            report = c.teardown([target])
            relay_clean = ev["steps"][-1].get("relay_cleanup") if ev["steps"] else None
            confirmed = all(v == "ok" for v in report[target].values())
            ev["cleanup_confirmed"] = confirmed
            step("cleanup", target=report[target], relay=relay_clean, confirmed=confirmed)
    ev["finished"] = datetime.now(timezone.utc).isoformat()
    (ROOT / "evidence").mkdir(exist_ok=True)
    out = ROOT / "evidence" / "gate1.json"
    out.write_text(json.dumps(ev, indent=2))
    print(f"\nGATE 1: {ev['result']}\nEvidence: {out}")


if __name__ == "__main__":
    main()
