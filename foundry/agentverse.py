"""Agentverse hosting API client: create, upload code, start, stop, delete, logs, search,
and independent invocation of a live agent through a short-lived relay agent."""
from __future__ import annotations

import json
import time

import requests

from . import config

RELAY_CODE = '''
from datetime import datetime, timezone
from uuid import uuid4
from uagents import Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement, ChatMessage, TextContent, chat_protocol_spec,
)

TARGET = __TARGET__
PROBE = __PROBE__
protocol = Protocol(spec=chat_protocol_spec)

@agent.on_event("startup")
async def go(ctx: Context):
    ctx.logger.info("RELAY_SENDING")
    await ctx.send(TARGET, ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
                                       content=[TextContent(type="text", text=PROBE)]))

@protocol.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
    for c in msg.content:
        t = getattr(c, "type", "")
        if t == "text":
            ctx.logger.info("FOUNDRY_RELAY_TEXT " + c.text.replace("\\n", " ")[:1500])
        elif t == "metadata":
            ctx.logger.info("FOUNDRY_RELAY_CARD " + str(dict(c.metadata).get("card_kind")))

@protocol.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    ctx.logger.info("FOUNDRY_RELAY_ACK")

agent.include(protocol, publish_manifest=True)
'''


class AgentverseError(RuntimeError):
    pass


class Client:
    def __init__(self, api_key: str | None = None, base: str | None = None):
        self.key = api_key or config.AGENTVERSE_API_KEY
        if not self.key:
            raise AgentverseError("AGENTVERSE_API_KEY is not set")
        self.base = (base or config.AGENTVERSE_URL).rstrip("/")
        self.h = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}

    def _req(self, method, path, **kw):
        r = requests.request(method, f"{self.base}{path}", headers=self.h, timeout=30, **kw)
        if r.status_code >= 300:
            raise AgentverseError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:400]}")
        try:
            return r.json()
        except ValueError:
            return r.text

    def create(self, name: str) -> str:
        data = self._req("POST", "/v1/hosting/agents", json={"name": name[:60]})
        addr = data.get("address") if isinstance(data, dict) else None
        if not addr:
            raise AgentverseError(f"no address in create response: {data}")
        return addr

    def upload(self, addr: str, code: str) -> None:
        files = [{"language": "python", "name": "agent.py", "value": code}]
        self._req("PUT", f"/v1/hosting/agents/{addr}/code", json={"code": json.dumps(files)})

    def update_profile(self, addr: str, name: str, readme: str, short: str) -> bool:
        try:
            self._req("PUT", f"/v1/hosting/agents/{addr}",
                      json={"name": name[:60], "readme": readme, "short_description": short[:200]})
            return True
        except AgentverseError:
            return False

    def start(self, addr: str) -> None:
        self._req("POST", f"/v1/hosting/agents/{addr}/start")

    def stop(self, addr: str) -> None:
        self._req("POST", f"/v1/hosting/agents/{addr}/stop")

    def delete(self, addr: str) -> None:
        self._req("DELETE", f"/v1/hosting/agents/{addr}")

    def logs(self, addr: str) -> str:
        r = requests.get(f"{self.base}/v1/hosting/agents/{addr}/logs/latest", headers=self.h, timeout=30)
        return r.text if r.status_code < 300 else ""

    def search(self, text: str, limit: int = 10) -> list:
        try:
            data = self._req("POST", "/v1/search/agents", json={"search_text": text, "limit": limit})
            return data.get("agents", []) if isinstance(data, dict) else []
        except AgentverseError:
            return []

    # ------------------------------------------------------------ independent invocation
    def invoke(self, target: str, message: str, wait_s: int = 150) -> dict:
        """Deploy a throwaway relay that sends `message` to `target` and records the reply
        from the platform logs. This is a real, third-party ACP call to the live agent."""
        relay = self.create(f"foundry-relay-{int(time.time()) % 100000}")
        code = RELAY_CODE.replace("__TARGET__", json.dumps(target)).replace("__PROBE__", json.dumps(message))
        out = {"relay": relay, "target": target, "sent": message, "texts": [], "cards": [], "acked": False}
        try:
            self.upload(relay, code)
            self.start(relay)
            t0 = time.time()
            while time.time() - t0 < wait_s:
                logs = self.logs(relay)
                if "FOUNDRY_RELAY_TEXT" in logs or "FOUNDRY_RELAY_CARD" in logs:
                    time.sleep(4)
                    logs = self.logs(relay)
                    out["texts"] = _grab(logs, "FOUNDRY_RELAY_TEXT ")
                    out["cards"] = _grab(logs, "FOUNDRY_RELAY_CARD ")
                    out["acked"] = "FOUNDRY_RELAY_ACK" in logs
                    out["latency_s"] = round(time.time() - t0, 1)
                    break
                time.sleep(4)
        finally:
            for fn in (self.stop, self.delete):
                try:
                    fn(relay)
                except AgentverseError:
                    pass
        return out


def _grab(logs: str, marker: str) -> list[str]:
    out = []
    try:
        entries = json.loads(logs)
        lines = [e.get("log_entry", "") if isinstance(e, dict) else str(e) for e in entries]
    except (ValueError, TypeError):
        lines = logs.splitlines()
    for line in lines:
        i = line.find(marker)
        if i >= 0:
            out.append(line[i + len(marker):].strip())
    return out
