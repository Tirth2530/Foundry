"""Foreman: FOUNDRY's ASI:One-facing agent (Agent Chat Protocol + Interactive Cards +
Payment Protocol). It answers instantly and runs long work in the background, pushing
progress and result cards back into the same conversation.

Conversation:
  request (or welcome form) -> Quartermaster quote (review card) -> approve [-> Stripe test payment]
  -> build with live progress -> verified result (detail card) -> publish to Agentverse
  -> independent live call -> evidence / evolution history / evolve / rollback
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from datetime import datetime, timezone
from uuid import uuid4

from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement, ChatMessage, MetadataContent, StartSessionContent, TextContent, chat_protocol_spec,
)
from uagents_core.contrib.protocols.payment import (
    CommitPayment, CompletePayment, Funds, RejectPayment, RequestPayment, payment_protocol_spec,
)

from .. import cards, config, ledger, pipeline, skills as sk
from ..roles import publisher, quartermaster
from . import payments

JOB_RE = re.compile(r"job_[0-9a-f]{6}")


def _now():
    return datetime.now(timezone.utc)


def chat(text: str | None = None, md: dict | None = None) -> ChatMessage:
    content = []
    if text:
        content.append(TextContent(type="text", text=text))
    if md:
        content.append(MetadataContent(type="metadata", metadata=md))
    return ChatMessage(timestamp=_now(), msg_id=uuid4(), content=content)


class Foreman:
    def __init__(self, agent: Agent):
        self.agent = agent
        self.jobs: dict = {}
        self.seen: set = set()
        self.paid_tx: set = set()
        self.loop = None
        self.ctx = None
        self.evolving = threading.Lock()
        self._load_jobs()

        chat_proto = Protocol(spec=chat_protocol_spec)
        pay_proto = Protocol(spec=payment_protocol_spec, role="seller")

        @agent.on_event("startup")
        async def _start(ctx: Context):
            self.loop = asyncio.get_running_loop()
            self.ctx = ctx
            ctx.logger.info(f"FOUNDRY Foreman live at {ctx.agent.address} (skills {sk.current_version()}, "
                            f"mode {'agent network' if pipeline.TRANSPORT else 'direct'})")

        @chat_proto.on_message(ChatMessage)
        async def _on_chat(ctx: Context, sender: str, msg: ChatMessage):
            await ctx.send(sender, ChatAcknowledgement(timestamp=_now(), acknowledged_msg_id=msg.msg_id))
            if str(msg.msg_id) in self.seen:
                return  # idempotent on ASI:One retries
            self.seen.add(str(msg.msg_id))
            text = " ".join(c.text for c in msg.content if isinstance(c, TextContent)).strip()
            started = any(isinstance(c, StartSessionContent) for c in msg.content)
            try:
                await self.route(ctx, sender, text, started)
            except Exception as e:  # noqa: BLE001 - never leave the user without a reply
                ctx.logger.exception("route failed")
                await ctx.send(sender, chat(f"Something failed on my side ({type(e).__name__}: {e}). "
                                            "Nothing was charged. Please try again."))

        @chat_proto.on_message(ChatAcknowledgement)
        async def _on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
            pass

        @pay_proto.on_message(CommitPayment)
        async def _on_commit(ctx: Context, sender: str, msg: CommitPayment):
            await self.on_commit(ctx, sender, msg)

        @pay_proto.on_message(RejectPayment)
        async def _on_reject(ctx: Context, sender: str, msg: RejectPayment):
            for j in self.jobs.values():
                if j.get("sender") == sender and j["status"] == "awaiting_payment":
                    self._update(j, status="cancelled")
            await ctx.send(sender, chat("Payment declined, so nothing will be built. Send a new request any time."))

        agent.include(chat_proto, publish_manifest=True)
        agent.include(pay_proto, publish_manifest=True)

    # ------------------------------------------------------------ persistence
    def _load_jobs(self):
        for p in config.RUNS.glob("job_*/job.json"):
            try:
                j = json.loads(p.read_text())
                self.jobs[j["job_id"]] = j
            except (ValueError, KeyError):
                continue

    def _update(self, job: dict, **kw):
        job.update(kw)
        d = config.RUNS / job["job_id"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "job.json").write_text(json.dumps(job, indent=1, default=str))

    def later(self, to: str, msg: ChatMessage):
        """Send from a worker thread into the agent's event loop."""
        asyncio.run_coroutine_threadsafe(self.ctx.send(to, msg), self.loop)

    # ------------------------------------------------------------ routing
    async def route(self, ctx, sender, text, started):
        sel = None
        if text.startswith("{"):
            try:
                sel = json.loads(text)
            except ValueError:
                sel = None
        action, jid = (sel or {}).get("action"), (sel or {}).get("job_id")
        if not action:
            action, jid = self.infer(text)
        if action == "submit_request":
            req = str(sel.get("request", "")).strip()
            budget = sel.get("budget")
            return await self.quote(ctx, sender, req, budget)
        if action in ("approve_quote", "reject_quote", "publish", "evidence") and jid not in self.jobs:
            return await ctx.send(sender, chat(f"I couldn't find job {jid}. Say 'status' to see your jobs."))
        handlers = {
            "approve_quote": lambda: self.approve(ctx, sender, self.jobs[jid]),
            "reject_quote": lambda: self.reject(ctx, sender, self.jobs[jid]),
            "publish": lambda: self.publish(ctx, sender, self.jobs[jid]),
            "evidence": lambda: self.evidence(ctx, sender, self.jobs[jid]),
            "status": lambda: self.status(ctx, sender),
            "history": lambda: self.history(ctx, sender),
            "evolve": lambda: self.evolve(ctx, sender),
            "rollback": lambda: self.rollback(ctx, sender, (sel or {}).get("version") or jid),
            "welcome": lambda: self.welcome(ctx, sender),
        }
        if action in handlers:
            return await handlers[action]()
        if len(text) < 25:
            return await self.welcome(ctx, sender)
        return await self.quote(ctx, sender, text, None)

    def infer(self, text: str):
        low = text.lower().strip()
        m = JOB_RE.search(low)
        jid = m.group(0) if m else None
        if jid:
            if any(w in low for w in ("reject", "cancel", "decline")):
                return "reject_quote", jid
            if "publish" in low or "deploy" in low:
                return "publish", jid
            if "evidence" in low or "detail" in low or "proof" in low:
                return "evidence", jid
            if any(w in low for w in ("approve", "confirm", "accept", "go ahead", "proceed")):
                return "approve_quote", jid
        m = re.match(r"rollback\s+(v\d{4})", low)
        if m:
            return "rollback", m.group(1)
        if low in ("status", "my jobs", "jobs") or low.startswith("status"):
            return "status", None
        if any(w in low for w in ("evolution", "history", "versions", "changelog", "what have you learned")):
            return "history", None
        if low in ("evolve", "teach", "improve", "improve yourself", "learn") or "run an evolution" in low:
            return "evolve", None
        if low in ("", "hi", "hello", "hey", "help", "start", "menu"):
            return "welcome", None
        return None, None

    # ------------------------------------------------------------ steps
    async def welcome(self, ctx, sender):
        v = sk.load()
        md = cards.form("What should FOUNDRY build?", [
            {"name": "request", "kind": "text", "label": "Describe the agent or multi-agent system", "required": True},
            {"name": "budget", "kind": "number", "label": "Spend cap in USD (optional)"},
        ], "Get a verified quote", {"action": "submit_request"})
        await ctx.send(sender, chat(
            "I'm FOUNDRY. I build Fetch.ai agents and multi-agent systems, prove they work with tests written by "
            "a different model that never sees the code, and publish them to Agentverse. I also improve my own "
            f"engineering process from every failure. I'm on skill version {v.version}. Say 'history' to see what "
            "I've learned, or describe what you need.", md))

    async def quote(self, ctx, sender, request, budget):
        if len(request) < 15:
            return await self.welcome(ctx, sender)
        v = sk.load()
        q = await asyncio.to_thread(pipeline.rpc, "quartermaster", "quote", lambda request, version:
                                    quartermaster.quote(request, sk.load(version)), request=request, version=v.version)
        if budget:
            try:
                q["cap_usd"] = round(min(float(budget), max(q["cap_usd"], 0.05)), 2)
            except (TypeError, ValueError):
                pass
        jid = pipeline.new_job_id()
        job = {"job_id": jid, "sender": sender, "request": request, "quote": q, "status": "quoted",
               "created": time.time(), "skills_version": v.version}
        self.jobs[jid] = job
        self._update(job)
        pay = "Stripe test checkout" if payments.enabled() else "demo mode (no charge)"
        md = cards.review(f"Build quote · {jid}", [
            ("Request", request[:160] + ("…" if len(request) > 160 else "")),
            ("Estimated model cost", f"${q['estimate_usd']:.3f} ({q['basis']})"),
            ("Hard spend cap", f"${q['cap_usd']:.2f}: work stops if reached"),
            ("Price", f"${q['price_usd']:.2f} via {pay}"),
            ("Builder / verifier", f"{v.route('builder')} builds · {v.route('examiner')} writes the tests independently"),
            ("Skill version", v.version),
            ("Hosting & test compute", f"{q['hosting']} · {q['test_compute']}"),
        ], ("Approve & build", {"action": "approve_quote", "job_id": jid}),
            ("Cancel", {"action": "reject_quote", "job_id": jid}))
        await ctx.send(sender, chat(f"Here's my quote for {jid}. Nothing runs until you approve.", md))

    async def reject(self, ctx, sender, job):
        self._update(job, status="cancelled")
        await ctx.send(sender, chat(f"Cancelled {job['job_id']}. Nothing was built or charged."))

    async def approve(self, ctx, sender, job):
        if job["status"] != "quoted":
            return await ctx.send(sender, chat(f"{job['job_id']} is already {job['status']}."))
        if payments.enabled():
            co = await asyncio.to_thread(payments.create_checkout, job["quote"]["price_usd"], job["job_id"],
                                         sender, job["request"])
            self._update(job, status="awaiting_payment", checkout_id=co["checkout_session_id"],
                         expected_cents=co["amount_cents"])
            kw = dict(accepted_funds=[Funds(currency="USD", amount=f"{job['quote']['price_usd']:.2f}",
                                            payment_method="stripe")],
                      recipient=str(ctx.agent.address), description=f"FOUNDRY verified build {job['job_id']}",
                      metadata={"stripe": co, "service": "foundry_build", "job_id": job["job_id"]})
            try:
                req = RequestPayment(**kw, deadline_seconds=1800, reference=job["job_id"])
            except Exception:  # noqa: BLE001 - older protocol versions lack these fields
                req = RequestPayment(**kw)
            await ctx.send(sender, req)
            ledger.append("payment_requested", job_id=job["job_id"], usd=job["quote"]["price_usd"])
            return await ctx.send(sender, chat("Complete the test-mode checkout and I'll start immediately."))
        await self.start_build(ctx, sender, job)

    async def on_commit(self, ctx, sender, msg: CommitPayment):
        tx = msg.transaction_id
        job = next((j for j in self.jobs.values() if j.get("checkout_id") == tx), None)
        if not job:
            return await ctx.send(sender, RejectPayment(reason="unknown checkout session"))
        if tx in self.paid_tx:
            return await ctx.send(sender, CompletePayment(transaction_id=tx))  # idempotent
        ok, why = await asyncio.to_thread(payments.verify, tx, job["job_id"], job["expected_cents"])
        if not ok:
            return await ctx.send(sender, RejectPayment(reason=why))
        self.paid_tx.add(tx)
        ledger.append("payment_completed", job_id=job["job_id"], tx=tx)
        await ctx.send(sender, CompletePayment(transaction_id=tx))
        self._update(job, status="quoted", paid=True)
        await self.start_build(ctx, sender, job)

    async def start_build(self, ctx, sender, job):
        self._update(job, status="building", started=time.time())
        await ctx.send(sender, chat(f"Approved. Building {job['job_id']} now. I'll post progress here."))
        threading.Thread(target=self._build_worker, args=(job,), daemon=True).start()

    def _build_worker(self, job):
        sender, jid = job["sender"], job["job_id"]

        def progress(stage, info=None):
            info = info or {}
            if stage == "plan":
                self.later(sender, chat("Architect is designing the system."))
            elif stage == "build":
                self.later(sender, chat(
                    f"Independent tests ready: {info.get('tests')} acceptance cases written by "
                    f"{info.get('provider')} from your request only (it never sees code), plus "
                    f"{info.get('adversarial')} adversarial probes. Builder is writing the code."))
            elif stage == "repair":
                self.later(sender, chat(f"Attempt {info['attempt']} failed verification, so the Builder is "
                                        f"repairing it. First failure: {info['why'][:220]}"))
        try:
            res = pipeline.build(job["request"], sk.load(job.get("skills_version")),
                                 budget_usd=job["quote"]["cap_usd"], job_id=jid, progress=progress)
        except Exception as e:  # noqa: BLE001
            self._update(job, status="error", error=str(e))
            return self.later(sender, chat(f"{jid} hit an unrecoverable error: {e}. Evidence is in runs/{jid}/."))
        recon = quartermaster.reconcile(job["quote"], res["cost"]["total_usd"])
        self._update(job, status=res["status"], verify=res.get("verify"), reconcile=recon,
                     agents=(res.get("artifact") or {}).get("agents"), error=res.get("error"))
        self.later(sender, self.result_message(job, res, recon))

    def result_message(self, job, res, recon) -> ChatMessage:
        v = res.get("verify") or {}
        ok = res["status"] == "verified"
        ex = res.get("examiner") or {}
        attempts = res.get("attempts", [])
        ctas = [("Show evidence", {"action": "evidence", "job_id": job["job_id"]}, not ok)]
        if ok:
            ctas.insert(0, ("Publish to Agentverse", {"action": "publish", "job_id": job["job_id"]}, True))
        ctas.append(("Teach FOUNDRY (evolve)", {"action": "evolve"}, False))
        ev = res.get("evidence", {})
        md = cards.detail(f"{'Verified' if ok else 'Not verified'} · {job['job_id']}", [
            ("Status", res["status"].upper()),
            ("Independent tests", f"{v.get('passed', 0)}/{v.get('cases', 0)} passed over ACP"),
            ("Test author", f"{ex.get('provider')} ({'independent family' if ex.get('independent') else 'same family'})"),
            ("Attempts", f"{len(attempts)} ({max(0, len(attempts) - 1)} self-repairs)"),
            ("Agents built", ", ".join((res.get("artifact") or {}).get("agents", [])) or "-"),
            ("Cost", f"${recon['actual_usd']:.3f} actual vs ${recon['quoted_usd']:.3f} quoted (cap ${recon['cap_usd']:.2f})"),
            ("Skill version", res["skills_version"]),
            ("Observed", " · ".join(ev.get("observed", []))[:400]),
            ("Inferred (not proven)", " · ".join(ev.get("inferred", []))[:300]),
        ], ctas)
        text = (f"{job['job_id']} is verified: every independent test passed." if ok else
                f"{job['job_id']} did not pass every independent test ({res.get('error') or 'see evidence'}). "
                "I won't publish unverified work.")
        return chat(text, md)

    async def evidence(self, ctx, sender, job):
        p = config.RUNS / job["job_id"] / "result.json"
        if not p.exists():
            return await ctx.send(sender, chat(f"{job['job_id']} has no evidence yet (status: {job['status']})."))
        res = json.loads(p.read_text())
        rows = []
        for a in res.get("attempts", []):
            if a.get("skipped_run"):
                rows.append((f"Attempt {a['attempt'] + 1}", "static gate failed: " +
                             "; ".join(sum(a["static"].values(), []))[:200]))
            else:
                vv = a.get("verify", {})
                rows.append((f"Attempt {a['attempt'] + 1}",
                             f"{vv.get('passed')}/{vv.get('cases')} tests · card audit issues: {len(a.get('card_audit') or [])}"))
        rec = [e for e in res.get("events", []) if "recovery" in e]
        rows += [("Recovery events", str(len(rec))),
                 ("Model spend by role", ", ".join(f"{k} ${v['usd']:.3f} ({v['provider']})"
                                                   for k, v in res["cost"]["by_role"].items())[:400]),
                 ("Evidence folder", f"runs/{job['job_id']}/ (code per attempt, transcripts, tests)")]
        ctas = [("Publish to Agentverse", {"action": "publish", "job_id": job["job_id"]}, True)] \
            if res["status"] == "verified" else []
        await ctx.send(sender, chat("Here's the full evidence trail, attempt by attempt.",
                                    cards.detail(f"Evidence · {job['job_id']}", rows, ctas)))

    async def publish(self, ctx, sender, job):
        if job["status"] != "verified":
            return await ctx.send(sender, chat(f"{job['job_id']} is {job['status']}. I only publish verified builds."))
        if not config.AGENTVERSE_API_KEY:
            return await ctx.send(sender, chat("Publishing needs AGENTVERSE_API_KEY on the FOUNDRY server."))
        if job.get("published"):
            return await ctx.send(sender, chat(f"Already live: {job['published']['entry']}"))
        self._update(job, status="publishing")
        await ctx.send(sender, chat("Publishing to Agentverse, then calling the live agent as an independent "
                                    "third party to confirm it behaves the same in production."))
        threading.Thread(target=self._publish_worker, args=(job,), daemon=True).start()

    def _publish_worker(self, job):
        sender = job["sender"]
        res = json.loads((config.RUNS / job["job_id"] / "result.json").read_text())
        exam = json.loads((config.RUNS / job["job_id"] / "examiner_tests.json").read_text())
        smoke = (exam.get("cases") or [None])[0]
        try:
            out = pipeline.rpc("publisher", "publish", publisher.publish, job=res, smoke_case=smoke)
        except Exception as e:  # noqa: BLE001
            self._update(job, status="verified")
            return self.later(sender, chat(f"Publishing failed: {e}. The verified build is unchanged; try again."))
        live = out["live_invocation"]
        self._update(job, status="verified", published=out)
        live_txt = ("skipped" if live.get("skipped") else
                    f"{'PASS' if live.get('passed') else 'FAIL'} in {live.get('latency_s', '?')}s · "
                    f"reply: {(live.get('texts') or ['(none)'])[0][:120]}")
        md = cards.detail(f"Live on Agentverse · {job['job_id']}", [
            ("Entry agent", out["entry"]),
            ("Profile", out["profile_url"]),
            ("Independent live call", live_txt),
            ("Agents deployed", str(len(out["agents"]))),
            ("Use it", "Open the profile or @mention the agent in ASI:One"),
            ("Claim it", f"Run `python -m foundry claim {job['job_id']}` with your own Agentverse key to deploy this "
                         "exact verified build into your account"),
        ], [("Show evidence", {"action": "evidence", "job_id": job["job_id"]}, False)])
        self.later(sender, chat(f"{job['job_id']} is live. Address: {out['entry']}", md))

    async def status(self, ctx, sender):
        mine = sorted((j for j in self.jobs.values() if j.get("sender") == sender), key=lambda j: -j["created"])[:8]
        if not mine:
            return await self.welcome(ctx, sender)
        items = [{"id": j["job_id"], "title": j["job_id"], "subtitle": j["status"],
                  "secondary_text": j["request"][:90], "cta_label": "Evidence",
                  "selection": {"action": "evidence", "job_id": j["job_id"]}} for j in mine]
        await ctx.send(sender, chat("Your recent jobs:", cards.carousel("Your FOUNDRY jobs", "most recent first", items)))

    async def history(self, ctx, sender):
        decisions = ledger.read("patch_decision")[-8:][::-1]
        cur = sk.current_version()
        if not decisions:
            return await ctx.send(sender, chat(f"I'm on {cur} and haven't run an evolution round yet. Say 'evolve' "
                                               "to start one."))
        items = []
        for d in decisions:
            ce, cu = d.get("candidate_eval", {}), d.get("current", {})
            promoted = d["decision"] == "promoted"
            items.append({
                "id": d["candidate"],
                "title": f"{d['candidate']} → {d.get('new_version')}" if promoted else f"{d['candidate']} rejected",
                "subtitle": f"{d['patch'].get('kind')} {d['patch'].get('file', '')}".strip(),
                "secondary_text": f"promotion set: {cu.get('solved')}/{cu.get('tasks')} → {ce.get('solved')}/{ce.get('tasks')}"
                                  f" · ${ce.get('usd_per_build', 0):.3f}/build · {d['reason'][:80]}",
                "badges": ["promoted" if promoted else "rolled back"],
                "cta_label": "Roll back to parent" if promoted else "Details",
                "selection": {"action": "rollback", "version": d["parent"]} if promoted else
                {"action": "history"},
            })
        await ctx.send(sender, chat(f"Current skill version: {cur}. Every change had to beat the sealed promotion "
                                    "set with zero regressions. Rejected changes were rolled back.",
                                    cards.carousel("How FOUNDRY has evolved", f"current: {cur}", items)))

    async def evolve(self, ctx, sender):
        if not self.evolving.acquire(blocking=False):
            return await ctx.send(sender, chat("An evolution round is already running. I'll report when it's done."))
        await ctx.send(sender, chat("Starting an evolution round: build the dev tasks, mine failures, propose one patch "
                                    "to my own skills, then test it on the sealed promotion set. This takes a while, "
                                    "so I'll post the decision here."))

        def work():
            try:
                out = pipeline.rpc("evolver", "round", lambda **_: _direct_round())
                log = (out or {}).get("log", [])
                self.later(sender, chat("Evolution round finished:\n" + "\n".join(log[-8:])))
            except Exception as e:  # noqa: BLE001
                self.later(sender, chat(f"Evolution round failed: {e}"))
            finally:
                self.evolving.release()
        threading.Thread(target=work, daemon=True).start()

    async def rollback(self, ctx, sender, version):
        try:
            sk.rollback(version)
        except FileNotFoundError:
            return await ctx.send(sender, chat(f"No skill version {version}."))
        ledger.append("rollback", to=version, by=sender)
        await ctx.send(sender, chat(f"Rolled back. FOUNDRY now builds with {version}."))


def _direct_round():
    from .. import evolve
    lines: list[str] = []
    evolve.run(1, say=lines.append)
    return {"log": lines}


def create_foreman_agent() -> Agent:
    kw = dict(name="foundry", seed=config.FOREMAN_SEED, port=config.FOREMAN_PORT, mailbox=True,
              publish_agent_details=True)
    try:
        return Agent(**kw, readme_path=str(config.ROOT / "README_AGENT.md"))
    except TypeError:
        return Agent(**kw)
