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

## The 16 agents

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

## Run it: gates first, then the show

```bash
python -m pytest -q tests          # offline + real-engine tests, $0: validators, pipeline, evolution, gate, rollback, chat flow
python -m foundry doctor           # packages + a 1-token call to each model + Agentverse key check
python -m foundry gate1            # GATE 1: create + deploy + independently invoke a new hosted agent  -> evidence/gate1.json
python -m foundry sandbox-build    # Docker sandbox for generated code (do this before evolve)
python -m foundry selftest         # GATE 2: the runner catches real defects in a known agent (no LLM)  -> evidence/gate2_selftest.json
python -m foundry build "Base64 tool. 'encode <text>' replies 'base64: <encoded>'..."   # one real verified build
python -m foundry evolve --estimate --dev-limit 7      # what one round will cost, before spending anything
python -m foundry evolve --rounds 1 --dev-limit 7 --max-usd 15   # GATE 3: one affordable round first
python -m foundry learn job_xxxxxx # live: one build's caught defects -> one skill change -> sealed gate decides
python -m foundry history          # every patch, the numbers, promoted or rolled back
python -m foundry serve            # the 16-agent network; Foreman goes live for ASI:One
```

**Connect the Foreman to ASI:One (once):** `serve` prints an *Agent inspector* link. Open it, click **Connect →
Mailbox**, then in Agentverse give the agent a handle (for example `@foundry`), an avatar, and paste
`README_AGENT.md` as its README. Chat with it from https://asi1.ai.

**At the end (run once):** `python -m foundry eval-final --repeats 2` compares the baseline `v0000`, the evolved
version, and (if evolution changed models or budget) a lessons-only arm, on the untouched final set with the same
budget cap per build. Then `python -m foundry report` writes `evidence/report.md` with all numbers,
computed from the ledger.

## What to say in ASI:One
- "Build an agent that converts km and mi and asks with a form card when the unit is missing" → quote → approve →
  live progress → verified card → **Publish** → live third-party call.
- "find an agent that checks ISBN-13 numbers" → Agentverse audit carousel: each match's listing stats vs what it
  actually did when called.
- Ask for something FOUNDRY has already published → the Scout offers the existing agent after re-testing it live
  (**Use this agent** for $0, or **Build a new one**).
- "history" → carousel of every self-change, with promotion-set scores, including rejected ones.
- "evolve" → runs one evolution round and reports the decision.
- "scoreboard" → baseline vs evolved, cost per build, changes proposed, promoted and rolled back.
- "rollback v0001" → move to an earlier skill version.

## Integrity and safety (audited)
- **Release gate:** a build is `verified` only if, on the FINAL artifact, every independent test passed, the static
  gate is clean, every card is valid and nothing failed to load. Tool settings can't skip any of it.
- **The verifier can't be weakened by evolution:** the static gate and independent tests are locked on, and evolution
  may not re-route the Examiner/Adversary to the builder's model family.
- **Sandbox:** generated code runs with an EMPTY environment (no API keys), in isolated mode (FOUNDRY not importable),
  in a temp dir with CPU/file limits. With `python -m foundry sandbox-build` it runs in Docker instead: no network,
  read-only root, 768MB, 1 CPU, all capabilities dropped, non-root, only the runner and job file mounted (the sealed
  tests are not reachable). The Integrator also statically forbids files, env, eval/exec and os/sys/subprocess.
  Docker is optional (`FOUNDRY_SANDBOX=docker` after `sandbox-build`); the default process sandbox already keeps API keys away from generated code.
- **Publish gate (transactional):** every agent must report `running` on Agentverse and the live entry agent
  must pass an independent test with a real output assertion. Any failure (an exception mid-deploy, an agent that
  never starts, a failed live check) tears down everything that publish created. The result records which
  stop/delete calls were confirmed, and the build never enters the reuse catalog.
- **Real collaboration, not just a claimed one:** the sandbox records every message agents send to each other.
  Multi-agent benchmark tasks require the named agents to exist AND the required ones to receive messages from
  another agent, so a single agent faking the outputs fails (`TOPOLOGY`). For any multi-agent build, the release
  gate fails if a peer agent is never used.
- **Agentverse hosting API:** one version per client (`AGENTVERSE_HOSTING_API=auto|v1|v2`). `auto` switches the
  whole client to v2 if v1 is gone. Each address remembers its version, so upload/start/stop/delete/logs never mix.
- **Budget:** each model call is checked against the cap BEFORE it is made. `evolve --estimate` prints the cost of a
  round; `--dev-limit N` and `--max-usd X` keep rounds affordable.
- **Authorization:** only the requesting address can approve, pay, publish, inspect or learn from its job.
  `evolve`/`rollback` from chat are admin-only (`FOUNDRY_ADMINS`; say `whoami` to get your address). Learning runs
  are rate-limited.
- **Single-use final set:** it can only be opened by `eval-final`, once. A second run needs `--reopen "<reason>"`,
  which is logged and shown in the report. This is an application-level control plus a content hash in a
  hash-chained ledger, not cryptographic sealing; in Docker mode generated code also can't read the files.
- **What the tests prove:** (the number of passing tests is NOT the number of real builds) `tests/test_offline.py`, `test_learn.py`, `test_scout.py`, `test_security.py` use a
  scripted model to prove the control logic for $0. `tests/test_real_engine.py` uses the REAL uAgents runtime and
  sandbox: correct vs defective agent, API keys not visible to generated code, sealed tests unreadable in Docker.
  The real proof of self-improvement is a live `evolve` run with your keys: its numbers come only from the ledger.

## Costs (measured, not guessed)
Model tokens are metered per role and per provider. Agentverse hosting and local sandbox compute are NOT metered
(shown as such on the quote). The quote shows the estimate, the hard cap, and afterwards actual vs quoted.
An evolution round is about 14 dev + 8 promotion builds (plus 8 baseline the first time). Watch
`python -m foundry history` for $/build.

## Honest limits
- FOUNDRY verifies behaviour on the executed tests. Behaviour outside them is labelled *inferred*.
- Promotion is evidence on 8 sealed tasks, not proof of general improvement. That's why there is an untouched final
  set and a baseline comparison.
- "Claim" = deploy the exact verified build into **your** Agentverse account
  (`CLAIM_AGENTVERSE_API_KEY=... python -m foundry claim job_xxxxxx`). It is not a transfer of FOUNDRY's copy.

## Layout
`foundry/` engine · `foundry/roles/` role logic · `foundry/agents/` uAgents (Foreman, network, payments) ·
`foundry/harness/` sandboxed ACP runner · `bench/` 14 dev + sealed 8 promotion + 8 final specs
(`tools/make_bench.py`) · `skills/versions/` every skill version · `evidence/` ledger, gates, report.
