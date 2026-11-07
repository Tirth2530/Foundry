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
python -m pytest -q tests          # 18 offline tests, $0: validators, pipeline, evolution, gate, rollback, chat flow
python -m foundry doctor           # packages + a 1-token call to each model + Agentverse key check
python -m foundry gate1            # GATE 1: create + deploy + independently invoke a new hosted agent  -> evidence/gate1.json
python -m foundry selftest         # GATE 2: the runner catches real defects in a known agent (no LLM)  -> evidence/gate2_selftest.json
python -m foundry build "Base64 tool. 'encode <text>' replies 'base64: <encoded>'..."   # one real verified build
python -m foundry evolve --rounds 3   # GATE 3: self-improvement against the sealed promotion set
python -m foundry history          # every patch, the numbers, promoted or rolled back
python -m foundry serve            # the 16-agent network; Foreman goes live for ASI:One
```

**Connect the Foreman to ASI:One (once):** `serve` prints an *Agent inspector* link. Open it, click **Connect →
Mailbox**, then in Agentverse give the agent a handle (for example `@foundry`), an avatar, and paste
`README_AGENT.md` as its README. Chat with it from https://asi1.ai.

**At the end (run once):** `python -m foundry eval-final --repeats 2` compares the baseline `v0000` with the evolved
version on the untouched final set. Then `python -m foundry report` writes `evidence/report.md` with all numbers,
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
- "rollback v0001" → move to an earlier skill version.

## Costs (measured, not guessed)
Every model call is metered per role. The quote shows the estimate, the hard cap, and afterwards actual vs quoted.
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
