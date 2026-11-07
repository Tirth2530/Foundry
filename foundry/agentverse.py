"""Agentverse hosting API client: create, upload code, start, stop, delete, logs, search,
and independent invocation of a live agent through a short-lived relay agent."""
from __future__ import annotations

import json
import time

import requests

from . import config

RELAY_CODE = '''
import asyncio
from datetime import datetime, timezone
from uuid import uuid4
from uagents import Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement, ChatMessage, TextContent, chat_protocol_spec,
)

PROBES = __PROBES__          # [{"i": int, "target": address, "send": text}]
GAP_S = 4                    # wait between probes to the same agent so multi-message replies stay attributed
queues, current, scheduled = {}, {}, set()
for p in PROBES:
    queues.setdefault(p["target"], []).append(p)
protocol = Protocol(spec=chat_protocol_spec)


async def send_next(ctx, target):
    q = queues.get(target) or []
    if not q:
        return
    p = q.pop(0)
    current[target] = p["i"]
    ctx.logger.info(f"FOUNDRY_RELAY_SEND {p['i']}")
    await ctx.send(target, ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
                                       content=[TextContent(type="text", text=p["send"])]))


async def later(ctx, target):
    await asyncio.sleep(GAP_S)
    scheduled.discard(target)
    await send_next(ctx, target)


@agent.on_event("startup")
async def go(ctx: Context):
    for target in list(queues):
        await send_next(ctx, target)


@protocol.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
    i = current.get(sender)
    if i is None:
        return
    for c in msg.content:
        t = getattr(c, "type", "")
        if t == "text":
            ctx.logger.info(f"FOUNDRY_RELAY_TEXT {i} " + c.text.replace("\\n", " ")[:1500])
        elif t == "metadata":
            ctx.logger.info(f"FOUNDRY_RELAY_CARD {i} " + str(dict(c.metadata).get("card_kind")))
    if queues.get(sender) and sender not in scheduled:
        scheduled.add(sender)
        asyncio.ensure_future(later(ctx, sender))


@protocol.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    if sender in current:
        ctx.logger.info(f"FOUNDRY_RELAY_ACK {current[sender]}")


agent.include(protocol, publish_manifest=True)
'''

CHAT_PROTOCOL_NAMES = ("AgentChatProtocol", "chat")


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

    def search_agents(self, text: str, *, limit: int = 15, semantic: bool = True, retries: int = 2) -> list[dict]:
        """Agentverse marketplace search (POST /v1/search/agents). Retries transient failures;
        raises AgentverseError if it keeps failing, so callers can report it honestly."""
        body = {"filters": {"state": ["active"], "category": [], "agent_type": [], "protocol_digest": []},
                "sort": "relevancy", "direction": "desc", "search_text": text[:200],
                "semantic_search": semantic, "rerank": True, "offset": 0, "limit": limit,
                "exclude_geo_agents": True}
        last = None
        for a in range(retries + 1):
            try:
                r = requests.post(f"{self.base}/v1/search/agents", headers=self.h, json=body, timeout=15)
                if r.status_code == 422 and semantic:  # older deployments reject newer fields
                    body = {k: v for k, v in body.items() if k not in ("semantic_search", "rerank", "exclude_geo_agents")}
                    continue
                if r.status_code >= 300:
                    raise AgentverseError(f"search HTTP {r.status_code}: {r.text[:200]}")
                data = r.json()
                return data.get("agents", []) if isinstance(data, dict) else []
            except (requests.RequestException, ValueError, AgentverseError) as e:
                last = e
                time.sleep(1.5 * (a + 1))
        raise AgentverseError(f"search failed after {retries + 1} tries: {last}")

    def search(self, text: str, limit: int = 10) -> list:
        try:
            return self.search_agents(text, limit=limit)
        except AgentverseError:
            return []

    # ------------------------------------------------------------ independent invocation
    def invoke_many(self, probes: list[dict], wait_s: int = 180) -> dict:
        """ONE throwaway relay agent sends every probe ({id, target, send}) over ACP (probes to the
        same agent go one at a time) and records the replies from the platform logs. These are
        real third-party calls to live agents. Returns {id: {texts, cards, acked, latency_s}}."""
        idx = {i: p for i, p in enumerate(probes)}
        payload = [{"i": i, "target": p["target"], "send": p["send"]} for i, p in idx.items()]
        relay = self.create(f"foundry-relay-{int(time.time()) % 100000}")
        out = {p["id"]: {"relay": relay, "target": p["target"], "sent": p["send"], "texts": [], "cards": [],
                         "acked": False, "latency_s": None} for p in probes}
        try:
            self.upload(relay, RELAY_CODE.replace("__PROBES__", json.dumps(payload)))
            self.start(relay)
            t0 = time.time()
            while time.time() - t0 < wait_s:
                time.sleep(4)
                logs = self.logs(relay)
                got = _indexed(logs, "FOUNDRY_RELAY_TEXT ") | _indexed(logs, "FOUNDRY_RELAY_CARD ")
                for i in got:
                    rec = out[idx[i]["id"]]
                    if rec["latency_s"] is None:
                        rec["latency_s"] = round(time.time() - t0, 1)
                if len(got) == len(probes):
                    time.sleep(5)  # let trailing card messages land
                    break
            logs = self.logs(relay)
            for kind, key in (("FOUNDRY_RELAY_TEXT ", "texts"), ("FOUNDRY_RELAY_CARD ", "cards")):
                for i, val in _indexed_values(logs, kind):
                    out[idx[i]["id"]][key].append(val)
            for i in _indexed(logs, "FOUNDRY_RELAY_ACK "):
                out[idx[i]["id"]]["acked"] = True
        finally:
            for fn in (self.stop, self.delete):
                try:
                    fn(relay)
                except AgentverseError:
                    pass
        return out

    def invoke(self, target: str, message: str, wait_s: int = 150) -> dict:
        return self.invoke_many([{"id": "p", "target": target, "send": message}], wait_s=wait_s)["p"]


def _lines(logs: str) -> list[str]:
    try:
        entries = json.loads(logs)
        return [e.get("log_entry", "") if isinstance(e, dict) else str(e) for e in entries]
    except (ValueError, TypeError):
        return logs.splitlines()


def _indexed_values(logs: str, marker: str):
    for line in _lines(logs):
        j = line.find(marker)
        if j >= 0:
            rest = line[j + len(marker):].strip()
            head, _, val = rest.partition(" ")
            if head.isdigit():
                yield int(head), val.strip()


def _indexed(logs: str, marker: str) -> set:
    return {i for i, _ in _indexed_values(logs, marker)}


def has_chat_protocol(hit: dict) -> bool:
    protos = hit.get("protocols") or []
    names = [(p.get("name") if isinstance(p, dict) else str(p)) or "" for p in protos]
    return not protos or any(any(n in x for n in CHAT_PROTOCOL_NAMES) for x in names)
