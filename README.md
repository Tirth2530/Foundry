# FOUNDRY: an evolutionary compiler for Fetch.ai multi-agent systems

![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)

**Track:** Fetch.ai "Agents That Prove Their Work". Primary statement: **The System That Gets Better**. Its independent
verifier also covers **Trust but Verify**.

Coding agents can already generate agents. But generated agents fail silently (ASI:One drops a card with one wrong
key), and the systems that build them rarely learn from those failures. FOUNDRY does three things:

0. **Searches Agentverse, then verifies.** A Scout queries the Agentverse marketplace several ways (3 model-written
   queries, semantic and lexical, with retries). It keeps only active, chat-capable agents and adds FOUNDRY's own
   catalog of agents it has verified. The top 3 matches are then **re-ranked by live evidence**: one relay agent calls
   each one over ACP with (a) a test written from *your* request by the independent Examiner ("does it do what you
   need?") and (b) a probe derived from its own README ("does it do what it claims?"). The platform's rating and
   success rate are shown next to what was actually observed. Only an agent that passes is offered for reuse ($0).
   If search is down, the catalog and the build path still work.
1. **Builds** real Fetch.ai agents and multi-agent systems from a request in ASI:One. A Quartermaster quotes the cost
   and enforces a spend cap first.
2. **Proves** they work. Acceptance tests are written by a *different model family* (Meta Llama) from the request
   only; it never sees the code, so it doesn't share the builder's (GPT) blind spots. The tests run over the real
   Agent Chat Protocol in an isolated sandbox. Every card is schema-audited. Only verified builds can be published,
   and after publishing FOUNDRY calls the live agent on Agentverse as an independent third party.
3. **Evolves its own engineering process.** Failures are mined into ONE versioned patch to FOUNDRY itself: a lesson,
   a tool, the routing, or the repair budget. The patch is adopted only if it solves more tasks on a **sealed
   promotion set with zero regressions**. Otherwise it is rolled back. A separate **final set** stays untouched until
   the end, and every time it's opened is recorded in a hash-chained ledger.

## Why this isn't "Agentverse's generator again"
Agentverse can already generate an agent from a prompt. FOUNDRY adds what a generator doesn't do:
- **Verifies:** a different model family writes tests from the request only; generated agents are run as Agentverse
  hosts them; every card is audited; only verified builds publish; the live agent is called by a third party.
- **Learns:** defects caught during a build become ONE change to FOUNDRY's own skills, kept only if it solves more
  *unseen* sealed tasks with zero regressions, and rolled back otherwise (`learn`, `evolve`).
- **Proves it improved:** the same budget cap per build for baseline vs evolved on an untouched final set, plus a
  lessons-only arm that keeps the baseline's models and budget, so the gain can't come from a bigger model or more
  retries. Rejected changes are shown, not hidden.

## Demo script (the six beats judges should see)
1. In ASI:One a judge asks for a new multi-agent app → Scout searches Agentverse first → quote card → approve.
2. Live build progress; the **independent verifier catches a real defect** (attempt 1 fails, with the reason).
3. FOUNDRY repairs the app → **Verified** card.
4. Tap **Teach FOUNDRY from this build** → the defect becomes one change to FOUNDRY's skills.
5. The change is tested on the **sealed promotion set** (tasks never seen while learning) → promoted or rolled back,
   with the numbers.
6. **Publish** → live third-party call → someone else @mentions the agent and uses it. Close on **scoreboard**:
   baseline v0000 vs the evolved version on the untouched final set, same budget, rejected changes included.

Line to remember: *"Other AI systems build agents. FOUNDRY builds agents, learns from its own failures, and proves
it's becoming a better engineer."*

## The agents (each role is its own uAgent; role agents accept tasks from the Foreman only)

| Agent | Job | Trust boundary |
|---|---|---|
| **Foreman** | ASI:One front door: ACP, interactive cards, Payment Protocol | only agent users talk to |
| Scout (ASI:One LLM) | searches Agentverse plus the verified catalog, live-tests the top 3 for need and for claims | reuse only after a live pass |
| Quartermaster | quote from measured history, hard spend cap, actual vs quoted | prices and caps every job |
| Architect (ASI:One LLM) | smallest system design | advisory only, marked *inferred* |
| Builder (GPT) | writes and repairs code with the current skill version | never sees tests |
| Spec Examiner (Llama) | acceptance tests from the request only | never sees code |
| Adversarial Tester (Llama) | empty, oversized, junk and malformed-selection probes | |
| Integrator | hosted-contract static gate (imports, `agent`, PEERS) | |
| Execution Runner | boots agents exactly as Agentverse hosts them, drives them over ACP | only agent that executes generated code |
| Card Validator | audits every emitted card against the ASI:One schema | |
| Recovery | bounded retries, provider failover, never retries a blown budget | |
| Failure Miner | clusters failures and finds one generalizable root cause | |
| Skill Evolver | proposes one patch to FOUNDRY's skills, tools or routing | never sees promotion/final tests |
| Promotion Judge | sealed promotion set, aggregates only, strict two-sided rule | holds the sealed set |
| Publisher | deploys to Agentverse, wires PEERS, writes READMEs, live third-party call | only agent with deploy rights |
| Ledger | append-only, hash-chained evidence | tamper-evident |

## Setup (about 10 minutes)

```bash
python3.11 -m venv .venv && source .venv/bin/activate      # Python 3.10-3.12
pip install -r requirements.txt
cp .env.example .env                                        # then fill in the keys below
```

| Key in `.env` | Where to get it |
|---|---|
| `AGENTVERSE_API_KEY` | https://agentverse.ai → sign in → profile menu → **API Keys** → create (all scopes) |
| `ASI_ONE_API_KEY` | https://asi1.ai → sign in → **Developer / API Keys** → create |
| `OPENAI_API_KEY` | https://platform.openai.com/api-keys (set `OPENAI_MODEL` to your strongest model and its prices) |
| `LLAMA_API_KEY` | https://llama.developer.meta.com → API keys. If your Meta credits are on another OpenAI-compatible host, set `LLAMA_BASE_URL` and `LLAMA_MODEL` |
| `FOUNDRY_SEED` | any long random string; this is the Foreman's identity, so keep it secret and stable |
| `STRIPE_*` (optional) | https://dashboard.stripe.com/test/apikeys (test mode). Without these, FOUNDRY runs in demo mode with no charge |
| `FOUNDRY_SANDBOX` | **required**: `docker` (after `sandbox-build`; mandatory for sealed sets) or `process` (dev only). No default |
| `FOUNDRY_PROJECT_CAP_USD` | total model spend ever allowed (restart-safe); see `.env.example` for provider / learn caps |

## Evidence status (read this first)

This repository contains **no live run evidence** (`evidence/` is empty). Everything below that says "tested" refers
to the levels in this table; nothing scripted or mocked is presented as a live result.

| Level | What runs | Where | Status in this repo |
|---|---|---|---|
| 1. Offline, scripted | real FOUNDRY control logic; model + runner replaced by scripts; Agentverse HTTP faked | `pytest` (stubbed uAgents) | **131 passed** |
| 2. Real uAgents | real runtime, real sandboxed runner, real ACP messages, real traffic taps | `tests/test_real_engine.py`, `selftest` | needs `pip install -r requirements.txt`; **not run here** |
| 3. Real models | GPT / Llama / ASI:One calls with metered spend | `build`, `learn`, `evolve` | needs keys; **not run** |
| 4. Agentverse live | deploy, live third-party ACP call, search-index discovery, confirmed teardown | `gate1`, `build --publish`, `recheck` | needs key; **not run** |
| 5. ASI:One end to end | a person in ASI:One drives quote → build → passport → publish → reuse | `serve` + asi1.ai | **not run** |

## Run it: gates first, then the show

```bash
python -m pytest -q tests          # levels 1-2 (level 2 auto-skips without the real uagents package)
python -m foundry doctor           # packages, 1-token call per model, Agentverse API (same resolution as deploy), sandbox
python -m foundry sandbox-build    # Docker sandbox for generated code (FOUNDRY_SANDBOX=docker)
python -m foundry gate1            # GATE 1: create + deploy + independently invoke + confirmed teardown (exit 1 on fail)
python -m foundry selftest         # GATE 2: runner catches real defects, observes a real 2-agent hand-off (exit 1 on fail)
python -m foundry lint             # specs, bench/MANIFEST.json, cross-split leakage, ledger chain (exit 1 on error)
python -m foundry build "<request>" --budget 2 [--publish]   # one verified build -> runs/<job>/passport.json
python -m foundry passport-verify runs/<job>/passport.json  # deterministic re-check against files + ledger
python -m foundry trace <job>      # ASI:One intent -> roles -> sandbox -> release gate -> publisher -> live result
python -m foundry recheck <job> --wait 300   # DEPLOYED -> DISCOVERABLE once Agentverse search returns the agent
python -m foundry recover          # tear down a deployment left half-finished by a crash (confirmed by a 404)
python -m foundry learn <job>      # one build's caught defects -> one skill change -> sealed promotion gate
python -m foundry evolve --estimate --dev-limit 7 && python -m foundry evolve --rounds 1 --dev-limit 7 --max-usd 15
python -m foundry history          # every patch, promoted or rejected, with numbers
python -m foundry serve            # the agent network; Foreman goes live for ASI:One
```
The full self-improvement protocol (Stages A–E, equal-budget comparison) is in `docs/EXPERIMENT.md`. The
judge-demo application is specified in `docs/SHOWCASE.md` (not built yet).

**Connect the Foreman to ASI:One (once):** `serve` prints an *Agent inspector* link. Open it, click **Connect →
Mailbox**, then in Agentverse give the agent a handle (for example `@foundry`), an avatar, and paste
`README_AGENT.md` as its README. Chat with it from https://asi1.ai.

**At the end (run once):** `python -m foundry eval-final --repeats 3` compares A baseline `v0000`, B lessons-only
and C full evolved on the untouched final set (same tasks, tests and $/build cap). `python -m foundry report` writes
`evidence/report.md` from the ledger.

## What to say in ASI:One
- "Build an agent that converts km and mi and asks with a form card when the unit is missing" → capability
  proposal card (A) → approve → live progress → result → **Proof Passport** card (B) → Approve publication →
  live third-party call → published (or "deployed, not yet discoverable").
- "permissions job_xxxxxx" · "cancel job_xxxxxx" · "passport job_xxxxxx" · "recheck job_xxxxxx"
- "find an agent that checks ISBN-13 numbers" → Agentverse audit carousel: listing stats vs what it did when called.
- "Teach FOUNDRY from this build" → **evolution verdict** card (C): failure, change, held-out comparison, transfer,
  equal budget, decision, with Compare versions / history / Roll back (admin).
- "history" · "scoreboard" · "evolve" (admin) · "rollback v0001" (admin)

## Integrity and safety
- **Release gate (fail-closed):** `verified` requires, on the FINAL artifact: every independent test passed, static
  gate clean, every card valid, no load errors, and for multi-agent systems **observed collaboration**: each
  required peer *processed* (handler ran, no exception) a message from another generated agent during a *passing*
  test. A delivery counts only when the send record and the processed-receive record match on sender, receiver and
  correlation id and both lie in a passing test's window. A missing tap, an unmatched receive or no test window
  gives **UNVERIFIED**, and the build is not verified. One agent pretending to be three fails.
- **User topology contract:** the required agents come from the user's own words (deterministic parser, no model)
  or from the request form. The contract is shown on the capability card, sealed with SHA-256, approved by the
  user's tap, and enforced as approved. Precedence: benchmark spec > user contract > Architect plan (fallback
  only). An Architect that proposes one agent for a multi-agent request cannot make the build pass.
- **Publication states:** `FAILED` (torn down) · `DEPLOYED` (running, profile written, live ACP call acknowledged
  **and** behaviourally correct, not yet in the search index) · `DISCOVERABLE` (also returned by Agentverse search).
  Only `DISCOVERABLE` is called published. Teardown is confirmed by a 404, otherwise reported as unconfirmed.
  Progress is written after every created agent, so `recover` can clean up after a crash; a retry never deploys a
  duplicate. Code containing any factory credential is refused before upload.
- **Budgets** (details and cross-process limits: `docs/BUDGETS.md`)**:** every model call reserves its worst case (input estimate + the **full** output-token ceiling at the
  configured price) atomically across job → evolution run → project, plus per-provider ceilings, before sending.
  Actual cost replaces the reservation afterwards. Unknown pricing is refused. Cancellation stops later stages.
  Estimated, calculated and provider-reported costs are kept separate (`evidence/usage.db`). Limit: a provider can
  still bill more than the reservation if it ignores `max_tokens` or bills asynchronously; overruns are recorded.
- **Authorization:** role agents accept `RoleTask` only from the Foreman's address (authenticated by the uAgents
  envelope) and refuse role confusion and skill paths outside the skill store; replies are bound to request id and
  expected sender. Users can act only on their own jobs; payment commits are checked against the job owner, verified
  with Stripe before work starts, and idempotent across retries and restarts. `evolve`/`rollback` are admin-only.
- **Sandbox:** `FOUNDRY_SANDBOX` must be set explicitly; there is no silent fallback. Sealed promotion/final sets
  require Docker (an override is possible only with a logged reason). The job file holds only agent code and the
  messages to send, never expectations. No credentials enter the sandbox or deployed code.
- **Benchmark integrity:** `bench/MANIFEST.json` freezes a SHA-256 and a content fingerprint per spec; `lint`
  detects changed files, duplicate and near-duplicate tasks across splits, and warns when the promotion set has been
  reused enough to risk overfitting. The final set is single-use and every opening is in the ledger. These are audit
  controls, not cryptographic secrecy: the files are plain JSON on disk.
- **Proof Passport:** every user build gets `runs/<job>/passport.json` (spec hash, code SHA-256, skill digest,
  models, harness digest, environment, examiner identity, tests, observed collaboration, security, cards, external
  action receipts, publication and discoverability, three kinds of spend, limitations, untested assumptions,
  evidence file hashes, related experiments, ledger anchor). Statuses are PASS · FAIL · UNVERIFIED · NOT TESTED and
  claims are OBSERVED or INFERRED; an INFERRED claim can never be PASS. `passport-verify` re-derives every digest.
- **Ledger:** hash-chained JSONL. Edits to earlier records are detectable; it is **not** signed, so someone who can
  rewrite the whole file can rebuild the chain.

## Costs (measured, not guessed)
Model tokens are metered per call, role and provider. Agentverse hosting and local sandbox compute are NOT metered
(said so on the quote and in the passport). The capability card shows the estimate, the hard cap, what happens when
the cap is reached, and the ongoing per-call cost; afterwards actual vs quoted with the three cost kinds.

## Honest limits
- FOUNDRY verifies behaviour on the executed tests. Behaviour outside them is labelled *inferred*.
- Promotion is evidence on 10 sealed tasks, not proof of general improvement; the final comparison is labelled a
  small sample below 30 builds per arm.
- "Claim" = deploy the exact verified build into **your** Agentverse account (`CLAIM_AGENTVERSE_API_KEY=...
  python -m foundry claim job_xxxxxx`). It is not a transfer of FOUNDRY's copy; no ownership-transfer API is used.
- Not implemented: GitHub showcase (spec in `docs/SHOWCASE.md`), fault-injection recovery metric, Sentry, neural
  weight adaptation (P2).

## Layout
`foundry/` engine · `foundry/roles/` role logic · `foundry/agents/` uAgents (Foreman, network, payments) ·
`foundry/harness/` sandboxed ACP runner · `foundry/passport.py` Proof Passport · `foundry/usage.py` usage ledger ·
`foundry/jobs.py` lifecycle + recovery · `foundry/trace.py` traces · `foundry/compare.py` controlled comparison ·
`bench/` 16 dev + sealed 10 promotion + 9 final specs and `MANIFEST.json` · `skills/versions/` every skill version ·
`evidence/` ledger, usage.db, gates, report · `docs/` engineering report, experiment runbook, showcase spec ·
`THIRD_PARTY_NOTICES.md` Dapta attribution (MIT).
