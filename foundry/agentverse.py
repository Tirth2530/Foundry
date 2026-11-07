"""Agentverse hosting API client: create, upload code, start, stop, delete, logs, search,
and independent invocation of a live agent through a short-lived relay agent."""
from __future__ import annotations

import json
import time

import requests

from . import config

RELAY_CODE = '''
import time
from datetime import datetime, timezone
from uuid import uuid4
from uagents import Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement, ChatMessage, TextContent, chat_protocol_spec,
)

# Agentverse hosted agents do NOT keep Python globals between messages: ALL state lives in ctx.storage.
PROBES = __PROBES__          # [{"i": int, "target": address, "send": text}]  (constant, safe as a global)
GAP_S = 4                    # gap between probes to the same agent so multi-part replies stay attributed
protocol = Protocol(spec=chat_protocol_spec)


def load(ctx):
    s = ctx.storage.get("relay_state")
    if not s:
        q = {}
        for p in PROBES:
            q.setdefault(p["target"], []).append(p["i"])
        s = {"queues": q, "current": {}, "answered": {}, "sends": {}}
    return s


def save(ctx, s):
    ctx.storage.set("relay_state", s)


def probe_index(s, sender):
    i = s["current"].get(sender)
    if i is None:
        own = [p["i"] for p in PROBES if p["target"] == sender]
        i = own[0] if own else None
    return i


async def send(ctx, target, i):
    text = next(p["send"] for p in PROBES if p["i"] == i)
    ctx.logger.info(f"FOUNDRY_RELAY_SEND {i}")
    await ctx.send(target, ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
                                       content=[TextContent(type="text", text=text)]))


async def start_next(ctx, s, target):
    if s["queues"].get(target):
        i = s["queues"][target].pop(0)
        s["current"][target] = i
        s["sends"][f"{target}|{i}"] = 1
        save(ctx, s)
        await send(ctx, target, i)


@agent.on_event("startup")
async def go(ctx: Context):
    s = load(ctx)
    for target in list(s["queues"]):
        if target not in s["current"]:
            await start_next(ctx, s, target)
    save(ctx, s)


@agent.on_interval(period=12.0)
async def tick(ctx: Context):
    s = load(ctx)
    now = time.time()
    for target, i in list(s["current"].items()):
        key = f"{target}|{i}"
        if key in s["answered"]:
            if now - s["answered"][key] >= GAP_S:
                await start_next(ctx, s, target)
            continue
        n = s["sends"].get(key, 1)
        if n < 12:  # a brand-new agent may not be routable yet: keep resending until it answers
            s["sends"][key] = n + 1
            save(ctx, s)
            ctx.logger.info(f"FOUNDRY_RELAY_RESEND {i} attempt {n + 1}")
            await send(ctx, target, i)


@protocol.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
    s = load(ctx)
    i = probe_index(s, sender)
    if i is None:
        return
    for c in msg.content:
        t = getattr(c, "type", "")
        if t == "text":
            ctx.logger.info(f"FOUNDRY_RELAY_TEXT {i} " + c.text.replace("\\n", " ")[:1500])
        elif t == "metadata":
            ctx.logger.info(f"FOUNDRY_RELAY_CARD {i} " + str(dict(c.metadata).get("card_kind")))
    s["answered"].setdefault(f"{sender}|{i}", time.time())
    save(ctx, s)


@protocol.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    s = load(ctx)
    i = probe_index(s, sender)
    if i is not None:
        ctx.logger.info(f"FOUNDRY_RELAY_ACK {i}")


agent.include(protocol, publish_manifest=True)
'''

CHAT_PROTOCOL_NAMES = ("AgentChatProtocol", "chat")


class AgentverseError(RuntimeError):
    pass


class Client:
    """Agentverse hosting + search. ONE hosting API version is used consistently:
    AGENTVERSE_HOSTING_API=auto (default) tries v1 on the first create and switches the whole client to v2
    if v1 is gone (404/405/410); v1 or v2 forces a version. Every address remembers the version that
    created it, so upload/start/stop/delete/logs never mix versions."""

    GONE = (404, 405, 410)

    def __init__(self, api_key: str | None = None, base: str | None = None, api: str | None = None):
        self.key = api_key or config.AGENTVERSE_API_KEY
        if not self.key:
            raise AgentverseError("AGENTVERSE_API_KEY is not set")
        self.base = (base or config.AGENTVERSE_URL).rstrip("/")
        self.h = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
        self.api = api or config.env("AGENTVERSE_HOSTING_API", "auto")
        self.ver: dict[str, str] = {}
        self.events: list[dict] = []

    # ------------------------------------------------------------ http
    def _req(self, method, path, retries: int = 2, **kw):
        last = None
        for a in range(retries + 1):
            try:
                r = requests.request(method, f"{self.base}{path}", headers=self.h, timeout=30, **kw)
            except requests.RequestException as e:
                last = AgentverseError(f"{method} {path} -> {type(e).__name__}: {e}")
                time.sleep(1.5 * (a + 1))
                continue
            if r.status_code in (429, 500, 502, 503, 504) and a < retries:
                last = AgentverseError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:200]}")
                time.sleep(2 * (a + 1))
                continue
            if r.status_code >= 300:
                err = AgentverseError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:400]}")
                err.status = r.status_code
                raise err
            try:
                return r.json()
            except ValueError:
                return r.text
        raise last

    def _v(self, addr: str | None = None) -> str:
        if addr and addr in self.ver:
            return self.ver[addr]
        return "v2" if self.api == "v2" else "v1"

    def _p(self, v: str, addr: str, suffix: str = "") -> str:
        return (f"/v1/hosting/agents/{addr}" if v == "v1" else f"/v2/hosting/{addr}") + suffix

    # ------------------------------------------------------------ hosting
    def create(self, name: str) -> str:
        data = None
        if self.api in ("auto", "v1"):
            try:
                data = self._req("POST", "/v1/hosting/agents", json={"name": name[:60]})
                v = "v1"
            except AgentverseError as e:
                if self.api == "v1" or getattr(e, "status", None) not in self.GONE:
                    raise
                self.api = "v2"
                self.events.append({"hosting_api": "switched to v2", "reason": str(e)[:200]})
        if data is None:
            data = self._req("POST", "/v2/hosting")
            v = "v2"
        addr = data.get("address") if isinstance(data, dict) else None
        if not addr:
            raise AgentverseError(f"no address in create response: {data}")
        self.ver[addr] = v
        if v == "v2":
            self.update_profile(addr, name, "", name)
        return addr

    def upload(self, addr: str, code: str) -> None:
        files = [{"language": "python", "name": "agent.py", "value": code}]
        self._req("PUT", self._p(self._v(addr), addr, "/code"), json={"code": json.dumps(files)})

    def update_profile(self, addr: str, name: str, readme: str, short: str) -> bool:
        try:
            if self._v(addr) == "v1":
                self._req("PUT", self._p("v1", addr), json={"name": name[:60], "readme": readme,
                                                             "short_description": short[:200]})
            else:
                self._req("PUT", f"/v2/agents/{addr}", json={"address": addr, "name": name[:60],
                                                               "profile": {"description": short[:200], "readme": readme}})
            return True
        except AgentverseError:
            return False

    def start(self, addr: str) -> None:
        self._req("POST", self._p(self._v(addr), addr, "/start"))

    def stop(self, addr: str) -> None:
        self._req("POST", self._p(self._v(addr), addr, "/stop"))

    def delete(self, addr: str) -> None:
        self._req("DELETE", self._p(self._v(addr), addr))

    def get(self, addr: str) -> dict:
        d = self._req("GET", self._p(self._v(addr), addr))
        return d if isinstance(d, dict) else {}

    def logs(self, addr: str) -> str:
        path = self._p(self._v(addr), addr, "/logs/latest" if self._v(addr) == "v1" else "/logs")
        try:
            r = requests.get(f"{self.base}{path}", headers=self.h, timeout=30)
        except requests.RequestException:
            return ""
        return r.text if r.status_code < 300 else ""

    def wait_running(self, addr: str, timeout: float = 60) -> bool:
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                d = self.get(addr)
                if d.get("running") and d.get("compiled") is not False:
                    return True
            except AgentverseError:
                pass
            time.sleep(3)
        return False

    def teardown(self, addrs) -> dict:
        """Stop and delete; report what was CONFIRMED, never assume."""
        report = {}
        for a in addrs:
            res = {}
            for op in ("stop", "delete"):
                try:
                    getattr(self, op)(a)
                    res[op] = "ok"
                except AgentverseError as e:
                    res[op] = f"failed: {str(e)[:120]}"
            report[a] = res
        return report

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
    def invoke_many(self, probes: list[dict], wait_s: int = 240) -> dict:
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
            if any(not r["texts"] and not r["cards"] for r in out.values()):
                tail = "\n".join(_lines(logs)[-40:])[-4000:]
                for r in out.values():
                    if not r["texts"] and not r["cards"]:
                        r["relay_logs_tail"] = tail
                        r["resends"] = len([l for l in _lines(logs) if "FOUNDRY_RELAY_RESEND" in l])
        finally:
            cleanup = self.teardown([relay])
            for rec in out.values():
                rec["relay_cleanup"] = cleanup[relay]
        return out

    def invoke(self, target: str, message: str, wait_s: int = 150) -> dict:
        return self.invoke_many([{"id": "p", "target": target, "send": message}], wait_s=wait_s)["p"]


def _lines(logs: str) -> list[str]:
    """Log lines OLDEST first (the API returns newest first)."""
    try:
        entries = json.loads(logs)
        if entries and isinstance(entries[0], dict) and "log_timestamp" in entries[0]:
            entries = sorted(entries, key=lambda e: e.get("log_timestamp", ""))
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
