# Runbook: the self-improvement experiment (Stages A–E)

**Status in this repository: the machinery is implemented and tested offline with a scripted model. No live run
has been executed yet, so there are no live numbers. Do not quote any until `evidence/` contains them.**

The claim to demonstrate: *a failure on application A produces one versioned change to FOUNDRY that helps it build
a genuinely different application B, under an equal budget, with no regressions.*

## Preconditions (once)
```bash
cp .env.example .env            # fill keys; keep FOUNDRY_SANDBOX=docker
python -m foundry sandbox-build # sealed sets refuse to run outside Docker
python -m foundry doctor        # exit 0 required
python -m foundry selftest      # GATE 2, exit 0 required (real uAgents + real runner)
python -m foundry lint          # specs, MANIFEST.json, leakage, ledger chain; exit 0 required
```

## Stage A: observe a genuine failure on application A
```bash
python -m foundry build "$(jq -r .request bench/dev/d15_issue_desk_multi.json)" --budget 2
python -m foundry trace job_xxxxxx          # where it failed: role, attempt, error category
```
A is a real build; its first attempt must have failed an independent test or the collaboration check (`attempts`
in `runs/<job>/result.json`). If attempt 1 passes, there is nothing to learn: pick another request, do not script one.

## Stage B: the application is repaired
The repair loop already ran inside the build. Evidence: `runs/<job>/attempts/00/{verify.json,feedback.txt}`
(failing input, output, root-cause text) and the last attempt's `verify.json` (successful retest).
`python -m foundry passport job_xxxxxx && python -m foundry passport-verify runs/job_xxxxxx/passport.json`.

## Stage C + D: repair FOUNDRY itself, evaluate on different tasks
```bash
FOUNDRY_LEARN_MAX_USD=10 python -m foundry learn job_xxxxxx
```
- Failure Miner → root cause; Evolver → ONE patch (lesson / tool / routing / repair budget); `000-*` lessons and the
  verifier (examiner model family, static gate, independent tests) are locked.
- The candidate and the current version are built on the **sealed promotion set** (10 tasks, 3 multi-agent),
  which the Evolver never sees, with the same models, tools, repair budget and **$/build cap**.
- The decision record (`python -m foundry history`, ledger `patch_decision`) contains `transfer`
  (how many promotion tasks are identical to A: must be 0; request similarity) and `equal_budget`.

## Stage E: promote or reject (policy fixed in `foundry/roles/judge.py`, before any run)
Promote iff the candidate solves strictly more promotion tasks AND loses none the current version solved, or solves
the same set ≥15% cheaper per build. Any infrastructure failure (error, budget stop, cancel) in either evaluation
⇒ inconclusive ⇒ not promoted. Rejected candidates stay in `skills/candidates/` and the ledger.

## Application B and the final comparison (run ONCE, after the methodology is frozen)
```bash
python -m foundry eval-final --repeats 3
python -m foundry report
```
Arms: A baseline `v0000` · B lessons-only (baseline models/budget + learned lessons) · C full evolved. Same 9 final
tasks (3 multi-agent), same acceptance tests, same $/build cap. Per-arm metrics are reported separately with sample
sizes and per-repeat spread (`foundry/compare.py`). 9 tasks × 3 repeats = 27 builds per arm: the report labels this
a small sample. Opening the final set again requires `--reopen "<reason>"` and is recorded.

## Cost control
Every model call reserves its worst case before it is sent. `FOUNDRY_PROJECT_CAP_USD` bounds everything (restart
safe, from `evidence/usage.db`); `evolve --max-usd` and `FOUNDRY_LEARN_MAX_USD` bound a run. Check spend with
`python -m foundry evolve --estimate` before starting.
