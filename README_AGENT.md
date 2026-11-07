# FOUNDRY: builds, proves and publishes Fetch.ai agents

![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)

Tell FOUNDRY what agent or multi-agent system you need. It first checks whether an existing Agentverse agent
already does it (by calling it live, not by trusting its README), quotes the cost with a hard spending limit,
builds it, and proves it works with independent tests written by a different AI model that never sees the code.
For multi-agent systems it also proves the agents really talked to each other. Every build gets a Proof Passport.
FOUNDRY publishes only verified builds, calls the live agent as a third party, and calls it "published" only once
Agentverse search returns it. FOUNDRY also improves its own engineering process: a defect it catches becomes one
versioned skill change that is kept only if it helps on different, unseen tasks with zero regressions.

## Try
- "Build an agent that checks ISBN-13 numbers and replies VALID or INVALID"
- "Build three agents: an investigator, an independent verifier and a publisher that asks me before posting"
- "passport job_xxxxxx" shows what was observed, what is only inferred, and what is untested
- "permissions job_xxxxxx" shows exactly what a job is allowed to do
- "cancel job_xxxxxx" stops a running build before its next paid step
- "history" shows how FOUNDRY has evolved, including rejected changes
- "status" shows your jobs

## Capabilities
agent builder, code generation, independent verification, testing, multi-agent systems, Agentverse publishing,
interactive cards, payments, self-improving, proof passport, evidence, software engineering, uAgents, ASI:One
