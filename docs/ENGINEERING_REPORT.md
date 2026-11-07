# FOUNDRY engineering report: P0 hardening + P1 evidence infrastructure

Branch `p0-hardening` (from `dc020a8`). 46 files changed, +4,400 / −315 lines.

## Read this first: what was and was not verified

| Level | Verified in this work? |
|---|---|
| Offline control logic (scripted model, scripted runner, faked Agentverse HTTP, uAgents stub) | **Yes: 107 passed** (baseline 37 passed) |
| Real uAgents runtime (`tests/test_real_engine.py`, `selftest`) | **No.** `uagents` is not installed in the build environment and package installs were blocked. 4 tests skip. `selftest` correctly exits 1 here. |
| Real model calls, Agentverse deployment, discoverability, Stripe, ASI:One | **No.** No credentials were available. Nothing here is live evidence. |

No live numbers are claimed anywhere. `evidence/` is intentionally empty. During this work a test wrote a fake
`evidence/gate1.json` ("HTTP 401 bad key") into the real evidence folder and it was committed. It was removed, and
the gate script now writes to the configured evidence directory. A test asserts the real folder is untouched.

**First thing to run on a machine with keys:** `pip install -r requirements.txt && FOUNDRY_SANDBOX=process python -m
pytest -q tests/test_real_engine.py && python -m foundry selftest`. The new receive-side traffic tap (below) has
**not** been exercised against the real uAgents package. If it does not install, every multi-agent build becomes
`UNVERIFIED`. That is fail-closed and safe, but it would block the demo.

## Phase 0: baseline
- 37 passed, 1 skipped (real-engine module) on the original code. uAgents absent; Docker CLI present but no daemon.
- Work on branch `p0-hardening`; `skills/versions/v0000` untouched; original benchmark files untouched (3 added).

## Phase 1 (P0): correctness and security

### 4.1 Multi-agent collaboration fails closed
- **Files:** `foundry/harness/runner.py`, `foundry/topology.py` (rewritten), `foundry/pipeline.py`, `foundry/__main__.py`.
- **Behaviour:** the runner now records the send side (sender, recipient, type, correlation id, time, delivery
  result) **and** the receive side. It wraps `Protocol.on_message` / `Agent.on_message` before generated code
  loads, so each handler run is logged with ok/exception. It also records per-case time windows. `topology.evaluate`
  returns PASS / FAIL / UNVERIFIED / NOT TESTED. PASS requires every required peer to have processed a message from
  another generated agent inside a passing case. Missing taps mean UNVERIFIED, which blocks `verified`; before this
  change it was only a warning note. Agent names in the Architect's plan become requirements, so one agent built for
  a three-agent design fails.
- **Tests (12, `test_collaboration.py`):** one agent pretending to be three; three named agents where the entry does
  everything; sends with no processing handler; a crashing peer; a peer used only during a failing case; missing tap
  → UNVERIFIED; end-to-end release gate blocks unobservable / under-built systems; the receive tap itself.
- **Unverified:** the taps against real uAgents (see top).

### 4.2 Publication requires real discoverability
- **Files:** `foundry/roles/publisher.py` (rewritten), `foundry/agentverse.py`, `foundry/agents/foreman.py`, CLI `recheck`.
- **Behaviour:** separate checks: deployed, running, profile, acp_ack, behaviour, discoverable. The states are
  FAILED, DEPLOYED (behaviour verified but not in the search index) and DISCOVERABLE (published). Discovery polls
  `/v1/search/agents` for the exact address and reports PENDING honestly. A profile update failure blocks release.
  Teardown is confirmed by a 404, otherwise it is listed as unconfirmed. Progress is persisted after each created
  agent, a retry never deploys a duplicate, and code containing any factory credential is refused.
- **Tests (9, `test_publication.py`)** plus updated `test_security.py`.
- **Unverified:** Agentverse search response shape, indexing delay, 404-after-delete semantics. Run `gate1`, then
  `build --publish`, then `recheck --wait 300`.

### 4.3 Enforceable financial limits
- **Files:** `foundry/llm.py` (budget core rewritten), `foundry/usage.py` (new, SQLite), `foundry/config.py`,
  `pipeline.py`, `evolve.py`, `roles/judge.py`, `roles/quartermaster.py`, `agents/network.py`, `agents/foreman.py`.
- **Behaviour:** each call reserves its worst case: input estimate (chars/3) plus the **full** `max_tokens` at the
  configured price, under one process lock, across job → evolution run → project, plus per-provider ceilings. The
  reservation is replaced by actual cost (provider-reported if returned, else calculated; missing usage is charged
  at the reservation). Unknown pricing is refused. Cancellation is checked before each stage and each call. Project
  and provider ceilings are rebuilt from `usage.db` on restart. `evolve --max-usd` is now a hard aggregate cap, and
  an evaluation that hits infrastructure failures is "inconclusive", never promoted. In network mode the job
  reserves the role agent's whole grant first; role-agent meters are detached so nothing is counted twice.
  Estimated, calculated and provider-reported costs are reported separately.
- **Tests (14, `test_budget.py`):** full-ceiling reservation; 6 concurrent calls against room for 2 → exactly 2 sent;
  reconciliation; missing usage; unknown pricing; parent cap across jobs; project/provider caps survive restart;
  cancellation; cancelled build runs no builder stage; usage-ledger idempotency and conflict; hard evolution cap;
  inconclusive evaluation; no code path bypasses the project ceiling.
- **Remaining limit (documented):** a provider that ignores `max_tokens` or bills asynchronously can exceed a
  reservation; overruns are recorded, not prevented.

### 4.4 Secure internal agents
- **Files:** `foundry/agents/network.py`, `foundry/agents/foreman.py`, `foundry/agents/payments.py`, `foundry/__main__.py`.
- **Behaviour:** role agents accept `RoleTask` only from the Foreman address. The uAgents envelope signature
  authenticates the sender address. Unknown ops, role confusion and skill paths outside the store are refused and
  logged. Transport results are accepted only for an outstanding request id, from the expected agent, with the
  expected role, once. Payment commits must come from the job owner, are verified with Stripe before the build
  starts, and are idempotent across retries and restarts (`paid_tx` persisted). Checkout creation uses an
  idempotency key.
- **Tests (11, `test_authorization.py`):** 5 sensitive roles refuse an outsider; role confusion; forged, unknown
  and replayed results; payment commit by another user; double commit → one build; failed verification → no build;
  other users cannot cancel, recheck, inspect or publish.

### 4.5 Operational correctness
- `doctor` uses `Client.probe_api()`, the same v1/v2 resolution as deployment, and checks the sandbox setting.
- `gate1` and `selftest` exit 1 on failure; `gate1` also requires confirmed teardown. `lint`, `passport-verify`,
  `recheck` and `recover` use meaningful exit codes.
- `FOUNDRY_SANDBOX` must be `docker` or `process`; there is no default and no silent downgrade. Sealed sets require
  Docker unless an override reason is given, and that override is logged. The job file never contains expectations.
- **Tests (6, `test_operations.py`).**

## Phase 2: live foundation. **Not executed** (no uAgents, Docker daemon or keys)
Commands: `python -m foundry sandbox-build && python -m foundry selftest && python -m foundry gate1`.

## Phase 3: research core. **Implemented, not run live**
- `learn` decisions now record `transfer`: how many evaluation tasks are identical to the source application
  (must be 0) and request similarity. They also record `equal_budget`: $/build cap, repair budgets, routing change.
- Runbook with exact commands and the frozen promotion policy: `docs/EXPERIMENT.md`.

## Phase 4: Dapta-grade infrastructure (selective; attribution in `THIRD_PARTY_NOTICES.md`)
| Integration | FOUNDRY file | Tests |
|---|---|---|
| A usage ledger | `usage.py` (idempotent SQLite, nano-dollars, cost sources, no prompts or secrets in metadata). Division of authority: usage.db = spend accounting, ledger.jsonl = decisions and events | `test_budget.py` |
| B dataset integrity | `bench.py`: `MANIFEST.json` (SHA-256 + fingerprint per spec), schema checks, exact and near-duplicate leakage across splits, promotion-reuse warning, final split locked once used | `test_offline.py` |
| C durable jobs | `jobs.py`: transition table enforced on every Foreman status change; restart marks running builds `interrupted` (never auto-resumed, so no repeated spend) and tears down half-finished deployments; CLI `recover` | `test_recovery.py` (4) |
| D comparison | `compare.py`: arms A/B/C, same tasks, tests and $/build; success rate with per-repeat spread, $/successful build, median runtime, incorrect-verification rate, regression rate, inter-agent success; deployment and fault-injection recovery reported as NOT TESTED; small-sample label; no composite score | `test_learn.py` |
| E observability | `trace.py`: `runs/<job>/trace.jsonl` connecting ASI:One intent → every role call → sandbox → release gate → publish → live call → discovery; redaction; stable error categories; CLI `trace`. Sentry not integrated | `test_trace.py` (2) |

Not imported: training stack, RunPod, vLLM, frontend, Supabase, receipt benchmark, factory controller.

## Phase 5: competition experience
- **Proof Passport** (`passport.py`, CLI `passport` / `passport-verify`). It is derived from the run folder and the
  ledger and contains every field in the brief. The verdict rules are enforced: an INFERRED claim can never be PASS,
  and VERIFIED requires passing tests, the static gate, cards, and observed collaboration for multi-agent builds.
  Validation re-derives code, evidence-file and body digests and checks the ledger anchor and chain.
  8 tests cover tampering with code, the passport, evidence files, recomputed digests and the ledger.
- **Cards:** A = capability proposal (existing agents, recommendation, cost, cap, what happens on exhaustion,
  ongoing cost, permissions; plus a `permissions` detail card). B = Proof Passport (with a self-check; Approve /
  Reject publication, Inspect evidence, Teach FOUNDRY). C = evolution verdict (failure, change, versions, held-out
  comparison, transfer, regressions, equal budget, cost, decision; Compare / history / Roll back). A test asserts
  every button on every card maps to a handled Foreman action (`test_cards.py`).
- Financial agent: the quote carries exhaustion behaviour, per-call cost and reuse cost; reconciliation keeps the
  three cost kinds separate.

## Phase 6: final evidence. **Not run** (final set untouched: no `final.lock`, zero recorded openings)

## Benchmark changes
+3 coupled tasks, expected values hand-checked: `d15_issue_desk_multi` (delegation, per-user cross-agent state,
idempotent filing, card selections), `d16_payment_ledger` (payment state transitions, idempotency, one-time
refund), and sealed `p10_finding_relay_multi` (investigate → independently verify → approve → publish exactly once).
Final set unchanged. Totals: 16 dev / 10 promotion / 9 final, with 2 / 3 / 3 multi-agent. **Risk:** there is no
reference implementation for the new tasks yet, so a spec ambiguity is possible. Build one reference agent per
task before the first paid run.

## Spend
$0. No paid call was made.

## Known risks / deferred
1. Receive tap and send-tap delivery status are unverified on real uAgents (fail-closed if broken).
2. Discovery relies on the search API returning `address`; the 120 s default wait may be short for indexing.
3. The Architect plan's agent names become required. A builder that renames agents gets repair feedback, which may
   cost an attempt.
4. Deferred: GitHub showcase (`docs/SHOWCASE.md` lists the 3 infrastructure changes it needs), fault-injection
   metric, Sentry, P2 weight adaptation, an ownership-transfer flow (none is claimed).

## Reproduce
```bash
python -m pytest -q tests               # 107 passed here; the real-engine module skips without uagents
python -m foundry lint                  # 35 specs, manifest match, no leakage, exit 0
```
