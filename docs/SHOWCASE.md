# Showcase: GitHub Issue Investigation Network

**Status: specified, NOT built, NOT tested.** This is the target for the judge demo. It needs live models, a
GitHub test repository and token, and the two infrastructure changes below. Nothing in this repository claims it
works yet.

## The request FOUNDRY receives (in ASI:One)
> Build a multi-agent system that accepts a GitHub issue, independently investigates whether the reported defect can
> be reproduced, and asks for my approval before publishing the verified findings back to GitHub.

## Required topology (enforced by `foundry/topology.py`, fail-closed)
`investigator` (entry) → `verifier` (independent: re-runs the reproduction itself, never trusts the investigator's
conclusion) → `publisher` (posts only after an approval card selection). `must_receive: [verifier, publisher]`.

## Acceptance contract (maps to the brief's 10 requirements)
| # | Requirement | How it is checked |
|---|---|---|
| 1 | real issue from an authorized test repo | test repo `<org>/foundry-showcase-sandbox` (to be created), issue created by the harness |
| 2 | retrieves issue data | reply quotes the issue title/number fetched from the API |
| 3 | scoped reproduction in a restricted environment | reproduction = a deterministic snippet in the issue body, run with stdlib only, time-limited |
| 4 | independent examination | `verifier` must process a message from `investigator` (observed traffic) and re-run the snippet |
| 5 | confirmed vs hypothesis | reply sections `CONFIRMED:` / `UNVERIFIED HYPOTHESIS:` checked by text checks |
| 6 | approval card | `review` card with selection `{"action":"post","issue":"<n>"}` |
| 7 | posts only after authorization | before the selection, GitHub shows no new comment (API read) |
| 8 | GitHub-side verification | after the selection, the comment exists with the FOUNDRY marker (API read) |
| 9 | inter-agent communication recorded | passport `collaboration: PASS` |
| 10 | failures and duplicates handled | second `post` selection → no second comment (API read); API 5xx → no partial post |

The miniature, offline version of this contract already exists as a sealed promotion task
(`p10_finding_relay_multi`: investigate → independently verify → approve → publish exactly once).

## Infrastructure still needed (deferred, with reasons)
1. **Network egress for one capability.** Generated code is statically forbidden from network access and the
   Docker sandbox runs with `--network none`. The showcase needs a separate, explicit capability profile:
   `urllib` allowed only to `api.github.com`, a Docker network with an egress proxy allowlisting that host, and a
   test-only token scoped to the sandbox repository. Not implemented: it widens the sandbox and must be designed and
   reviewed on its own.
2. **Secrets on Agentverse.** The deployed `publisher` needs the GitHub token as an Agentverse agent secret, never in
   code (the publisher now refuses to deploy code containing any factory credential).
3. **External-action receipts in the passport.** `external_actions` already records live ACP calls and payments; a
   GitHub comment id + URL read back from the API would be added the same way.

## Commands once (1)–(3) exist
```bash
python -m foundry build "<request above>" --budget 3 --publish
python -m foundry passport-verify runs/<job>/passport.json
python -m foundry recheck <job> --wait 300
```
Then, from a **second** ASI:One account, @mention the published entry agent with a new issue URL and confirm the
comment appears on GitHub only after approving the card.
