# Third-party notices

## Dapta

- Source: https://github.com/adiv-1/Dapta (studied at commit `74238458aff30d8991072cd338147d57aa9c3c55`)
- License: MIT (full text below)
- Copyright (c) 2026 Aditya Venkat

FOUNDRY did **not** import the Dapta application. We studied five modules and re-implemented the patterns that fit
FOUNDRY, in FOUNDRY's own code. Short fragments follow Dapta's code closely; those files carry an attribution
header and are covered by the MIT notice below.

| FOUNDRY file | Dapta source | What was adapted |
|---|---|---|
| `foundry/usage.py` | `backend/dapta/ledger.py` | SQLite usage ledger with idempotent insert-or-verify on a primary key (`LedgerConflict` → `UsageConflict`), integer nano-dollar money columns, explicit cost sources, refusal of prompt/credential fields in stored metadata |
| `foundry/jobs.py` | `backend/dapta/jobs.py`, `backend/dapta/cancellation.py` | explicit transition table, `interrupted` state for work whose process died, cancellation-requested kept distinct from cleanup-completed, bounded recovery that never re-runs a side-effecting step |
| `foundry/trace.py` | `backend/dapta/observability.py` | stable, content-free error categories; redaction of secret-looking keys before anything is written |
| `foundry/compare.py` | `backend/dapta/comparisons.py` | experimental design only: fixed task set and conditions, identical measurement per arm, per-condition summaries, no blended score |
| `foundry/bench.py` (`manifest`, `fingerprint`, `integrity`) | `backend/dapta/data.py` | SHA-256 file hashes, content fingerprints for cross-split duplicate detection, frozen manifest check |

Not used: Dapta's Qwen/QLoRA training stack, RunPod orchestration, vLLM server, frontend, Supabase storage,
receipt benchmark and factory controller.

### MIT License (Dapta)

```
MIT License

Copyright (c) 2026 Aditya Venkat

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Python dependencies

`uagents`, `uagents-core` (Fetch.ai, Apache-2.0), `requests` (Apache-2.0), `stripe` (MIT), `pytest` (MIT),
`pydantic` (MIT). Installed from PyPI via `requirements.txt`; not vendored.
