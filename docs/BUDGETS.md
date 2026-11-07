# Spending limits: how they are enforced, and where they stop

## Protocol (one process)
1. **Reserve before sending.** A model call reserves its worst case: estimated input (characters / 3) plus the
   *full* `max_tokens` at the configured price. The reservation is made in the job's meter **and every ancestor**
   (evolution run, project ceiling `FOUNDRY_PROJECT_CAP_USD`) plus the provider ceiling. If any level would exceed
   its cap, nothing is reserved and no request is sent. Unknown pricing is refused.
2. **Settle after.** The reservation is released and the actual cost committed in the same locked step:
   provider-reported if the API returns a cost, otherwise calculated from tokens. If the provider omits usage, the
   full reservation is charged.
3. **Remote role agents (network mode).** Before `pipeline.rpc` sends a `RoleTask`, the job reserves a **grant**
   equal to its remaining headroom. The role agent runs with a meter capped at that grant. When the reply arrives,
   `CostMeter.settle_grant` releases the grant **and** commits the reported usage under one lock. No other job can
   take the released headroom before the spend is booked (regression: `tests/test_budget_network.py`).
   - If the role agent never reports usage (timeout, crash, lost reply, transport exception), its spend is
     unknown, so the **whole grant** is charged as an `unreconciled` call, never zero.
4. **Cancellation** is checked before every stage and every call.

All budget state in a process is guarded by one lock (`llm._BUDGET_LOCK`), so check-and-reserve is atomic across
threads and cannot deadlock.

## Known limits (not hidden)
- **Cross-process.** The lock, the reservations and the in-memory project meter belong to ONE process. `serve` runs
  the Foreman and all role agents in one process (split and bureau modes), so they share one budget. Two separate
  FOUNDRY processes at the same time do **not** see each other's reservations. For example, `serve` plus a CLI
  `evolve`, or role agents moved to another machine. Each process reads past spend from `evidence/usage.db` only
  when its project meter is first created. Together they can exceed `FOUNDRY_PROJECT_CAP_USD` by up to what the
  other process spends while both run. Run one FOUNDRY process per budget, or rely on the provider's own hard limit
  (for example an OpenAI project budget) as the outer bound.
- **Remote role agents in another process** stay bounded per call, because their meter is capped at the grant. But
  their usage reaches the requesting job only through the reply. A lost reply is charged at the full grant, so
  accounting stays conservative and the cap is not exceeded.
- **Provider overruns.** A provider that ignores `max_tokens`, or bills later or asynchronously, can charge more
  than the reservation. The overrun is committed and recorded in `overruns`; it is not prevented. This is the only
  in-process way a cap can be exceeded.
- **Grants serialize work near the cap.** A grant reserves the job's whole remaining headroom. When that equals the
  project's remaining headroom, a second concurrent job is refused (`budget_exceeded`) instead of waiting, even if
  the first job will not use all of it. This fails closed. Keep per-build caps well below the project cap so grants
  can coexist (pinned by `test_known_limit_a_grant_as_large_as_project_headroom_serialises_jobs`).
- **Not metered:** Agentverse hosting, local sandbox compute.
