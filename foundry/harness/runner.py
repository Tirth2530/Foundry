"""Execution Runner (child process). Boots generated agents exactly as Agentverse hosts them
(a pre-created global `agent`, plus PEERS for multi-agent systems) inside a local uAgents
Bureau, then drives each test case from its own fresh client agent over the real Agent Chat
Protocol. It only RECORDS what happened; judging is done by the parent (foundry.dsl).

Usage: python -I foundry/harness/runner.py job.json   ->  prints one line "FOUNDRY_RESULT:{json}"
Isolation: separate process, hard wall-clock limit, no endpoints (nothing is registered on
the public Almanac during tests).
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from uuid import uuid4

_emit_lock = threading.Lock()
_emitted = False


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _emit(payload: dict) -> None:
    global _emitted
    with _emit_lock:
        if _emitted:
            return
        _emitted = True
        sys.stdout.write("FOUNDRY_RESULT:" + json.dumps(payload, default=str) + "\n")
        sys.stdout.flush()
        os._exit(0)


MAX_TRAFFIC = 2000  # bound the evidence a misbehaving system can produce


def correlation_id(message) -> str:
    """Stable id for one message, computed identically on the send and the receive side, so a send can be
    matched to the handler run that processed it. ACP messages carry msg_id; custom models are hashed."""
    for attr in ("msg_id", "acknowledged_msg_id", "rid"):
        v = getattr(message, attr, None)
        if v:
            return f"{attr}:{v}"
    try:
        body = message.model_dump_json() if hasattr(message, "model_dump_json") else message.json()
    except Exception:  # noqa: BLE001
        body = repr(message)
    import hashlib
    return "sha:" + hashlib.sha256(f"{type(message).__name__}|{body}".encode()).hexdigest()[:16]


def _ctx_address(ctx) -> str:
    agent = getattr(ctx, "agent", None)
    return str(getattr(agent, "address", None) or getattr(ctx, "address", None))


def _install_traffic_tap(traffic: list) -> bool:
    """SEND side: record every ctx.send (sender, destination, type, correlation id, time, delivery result)."""
    try:
        import uagents.context as uc
    except Exception:  # noqa: BLE001
        return False
    patched = False
    for obj in list(vars(uc).values()):
        fn = obj.__dict__.get("send") if isinstance(obj, type) else None
        if fn is None or not asyncio.iscoroutinefunction(fn):
            continue

        def wrap(orig):
            async def send(self, destination, message, *a, **kw):
                rec = None
                try:
                    me = getattr(getattr(self, "agent", None), "address", None) or getattr(self, "address", None)
                    rec = {"from": str(me), "to": str(destination), "type": type(message).__name__,
                           "corr": correlation_id(message), "t": time.time()}
                    if len(traffic) < MAX_TRAFFIC:
                        traffic.append(rec)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    out = await orig(self, destination, message, *a, **kw)
                except BaseException as e:
                    if rec is not None:
                        rec["send_error"] = f"{type(e).__name__}: {str(e)[:120]}"
                    raise
                if rec is not None:
                    status = getattr(out, "status", None)
                    rec["delivery"] = str(getattr(status, "value", status)) if status is not None else None
                return out
            return send
        setattr(obj, "send", wrap(fn))
        patched = True
    return patched


def install_receive_tap(classes, received: list) -> bool:
    """RECEIVE side: wrap the on_message decorators of uagents Protocol/Agent BEFORE generated code runs, so
    every handler records (recipient, sender, type, correlation id, time, ok/exception) when it actually
    PROCESSES a message. A send alone is not proof of collaboration; a handler that ran is."""
    import functools
    patched = False
    for cls in classes:
        orig = getattr(cls, "on_message", None)
        if orig is None or getattr(orig, "_foundry_tapped", False):
            continue

        def make(orig):
            def on_message(self, model, *a, **kw):
                deco = orig(self, model, *a, **kw)

                def register(fn):
                    @functools.wraps(fn)
                    async def handler(ctx, sender, msg, *ha, **hkw):
                        rec = {"to": _ctx_address(ctx), "from": str(sender), "type": type(msg).__name__,
                               "corr": correlation_id(msg), "t": time.time()}
                        try:
                            result = await fn(ctx, sender, msg, *ha, **hkw)
                            rec["ok"] = True
                            return result
                        except BaseException as e:
                            rec["ok"] = False
                            rec["error"] = f"{type(e).__name__}: {str(e)[:160]}"
                            raise
                        finally:
                            if len(received) < MAX_TRAFFIC:
                                received.append(rec)
                    return deco(handler)
                return register
            on_message._foundry_tapped = True
            return on_message
        setattr(cls, "on_message", make(orig))
        patched = True
    return patched


def main(job_path: str) -> None:
    job = json.load(open(job_path))
    turn_timeout = float(job.get("turn_timeout", 15))
    cases = job.get("cases", [])
    hard_limit = float(job.get("hard_limit", 20 + turn_timeout * max(1, sum(len(c["turns"]) for c in cases))))

    from uagents import Agent, Bureau, Context, Protocol  # noqa: F401
    from uagents_core.contrib.protocols.chat import (
        ChatAcknowledgement, ChatMessage, TextContent, chat_protocol_spec,
    )

    traffic: list = []
    received: list = []
    tap = _install_traffic_tap(traffic)
    rtap = install_receive_tap([Protocol, Agent], received)
    t_start = time.time()
    nonce = uuid.uuid4().hex[:8]
    bureau = Bureau(port=_free_port())
    agents = {a["name"]: Agent(name=f"gen-{a['name']}", seed=f"foundry-gen-{nonce}-{a['name']}")
              for a in job["agents"]}
    peers = {n: a.address for n, a in agents.items()}
    load_errors: dict = {}
    for spec in job["agents"]:
        g = {"agent": agents[spec["name"]], "PEERS": dict(peers),
             "__name__": f"foundry_generated_{spec['name']}"}
        try:
            exec(compile(spec["code"], f"<agent:{spec['name']}>", "exec"), g)
        except BaseException:  # noqa: BLE001 - we want every load failure as evidence
            load_errors[spec["name"]] = traceback.format_exc(limit=4)[-1500:]
        bureau.add(agents[spec["name"]])

    entry = job.get("entry") or job["agents"][0]["name"]
    base = {"peers": peers, "entry": entry, "load_errors": load_errors, "traffic_tap": tap,
            "receive_tap": rtap, "t0": t_start}
    windows: list = [None] * len(cases)
    inv = {v: k for k, v in peers.items()}

    def _name(addr):
        return inv.get(addr, "client")

    def named_traffic():
        """Deduplicated edges (for humans) - the full records are in traffic_log / received_log."""
        seen, out = set(), []
        for t in traffic:
            key = (_name(t["from"]), _name(t["to"]), t["type"])
            if key not in seen:
                seen.add(key)
                out.append({"from": key[0], "to": key[1], "type": key[2]})
        return out

    def logs():
        sent = [{**t, "from": _name(t["from"]), "to": _name(t["to"]), "t": round(t["t"] - t_start, 3)}
                for t in traffic]
        recv = [{**r, "from": _name(r["from"]), "to": _name(r["to"]), "t": round(r["t"] - t_start, 3)}
                for r in received]
        wins = [None if w is None else [round(w[0] - t_start, 3), round(w[1] - t_start, 3)] for w in windows]
        return {"traffic": named_traffic(), "traffic_log": sent, "received_log": recv, "case_windows": wins}
    if entry in load_errors or not cases:
        _emit({**base, "cases": [], **logs()})

    results: list = [None] * len(cases)
    states: list = []

    def finish_if_done():
        if all(r is not None for r in results):
            for st, r in zip(states, results):
                for t in r["turns"]:
                    t["acked"] = t.get("msg_id") in st["acks"]
            _emit({**base, "cases": results, **logs()})

    def serialize(msg) -> dict:
        texts, mds, end = [], [], False
        for c in msg.content:
            t = getattr(c, "type", "")
            if t == "text":
                texts.append(c.text)
            elif t == "metadata":
                mds.append({k: v for k, v in dict(c.metadata).items()})
            elif t == "end-session":
                end = True
        return {"texts": texts, "metadata": mds, "end_session": end}

    async def run_case(ctx, i: int, case: dict, st: dict):
        await asyncio.sleep(1.5 + 0.05 * i)
        turns = []
        case_t0 = time.time()
        for turn in case["turns"]:
            start = len(st["msgs"])
            mid = uuid4()
            t0 = time.time()
            try:
                await ctx.send(peers[entry], ChatMessage(
                    timestamp=datetime.now(timezone.utc), msg_id=mid,
                    content=[TextContent(type="text", text=turn["send"])]))
            except Exception as e:  # noqa: BLE001
                turns.append({"sent": turn["send"], "error": f"send failed: {e}"})
                continue
            while time.time() - t0 < turn_timeout and len(st["msgs"]) == start:
                await asyncio.sleep(0.05)
            first = st["msgs"][start][0] if len(st["msgs"]) > start else None
            if first:
                await asyncio.sleep(0.9)  # collect follow-up messages (e.g. text then card)
            turns.append({
                "sent": turn["send"], "msg_id": str(mid),
                "replies": [serialize(m) for _, m in st["msgs"][start:]],
                "latency_ms": int((first - t0) * 1000) if first else None,
            })
        windows[i] = (case_t0, time.time())
        results[i] = {"name": case.get("name", f"case{i}"), "turns": turns}
        finish_if_done()

    for i, case in enumerate(cases):
        client = Agent(name=f"client{i}", seed=f"foundry-client-{nonce}-{i}")
        proto = Protocol(spec=chat_protocol_spec)
        st = {"msgs": [], "acks": set()}
        states.append(st)

        def make(client=client, proto=proto, st=st, i=i, case=case):
            @proto.on_message(ChatMessage)
            async def on_msg(ctx: Context, sender: str, msg: ChatMessage):
                st["msgs"].append((time.time(), msg))
                await ctx.send(sender, ChatAcknowledgement(
                    timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))

            @proto.on_message(ChatAcknowledgement)
            async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
                st["acks"].add(str(msg.acknowledged_msg_id))

            client.include(proto)

            @client.on_event("startup")
            async def start(ctx: Context):
                asyncio.ensure_future(run_case(ctx, i, case, st))

        make()
        bureau.add(client)

    def watchdog():
        time.sleep(hard_limit)
        partial = [r or {"name": c.get("name"), "turns": [], "error": "case did not finish"}
                   for r, c in zip(results, cases)]
        _emit({**base, "cases": partial, "watchdog": True, **logs()})

    threading.Thread(target=watchdog, daemon=True).start()
    bureau.run()


if __name__ == "__main__":
    main(sys.argv[1])
