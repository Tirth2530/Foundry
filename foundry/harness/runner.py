"""Execution Runner (child process). Boots generated agents exactly as Agentverse hosts them
(a pre-created global `agent`, plus PEERS for multi-agent systems) inside a local uAgents
Bureau, then drives each test case from its own fresh client agent over the real Agent Chat
Protocol. It only RECORDS what happened; judging is done by the parent (foundry.dsl).

Usage: python -m foundry.harness.runner job.json   ->  prints one line "FOUNDRY_RESULT:{json}"
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


def main(job_path: str) -> None:
    job = json.load(open(job_path))
    turn_timeout = float(job.get("turn_timeout", 15))
    cases = job.get("cases", [])
    hard_limit = float(job.get("hard_limit", 20 + turn_timeout * max(1, sum(len(c["turns"]) for c in cases))))

    from uagents import Agent, Bureau, Context, Protocol  # noqa: F401
    from uagents_core.contrib.protocols.chat import (
        ChatAcknowledgement, ChatMessage, TextContent, chat_protocol_spec,
    )

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
    base = {"peers": peers, "entry": entry, "load_errors": load_errors}
    if entry in load_errors or not cases:
        _emit({**base, "cases": []})

    results: list = [None] * len(cases)
    states: list = []

    def finish_if_done():
        if all(r is not None for r in results):
            for st, r in zip(states, results):
                for t in r["turns"]:
                    t["acked"] = t.get("msg_id") in st["acks"]
            _emit({**base, "cases": results})

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
        _emit({**base, "cases": partial, "watchdog": True})

    threading.Thread(target=watchdog, daemon=True).start()
    bureau.run()


if __name__ == "__main__":
    main(sys.argv[1])
