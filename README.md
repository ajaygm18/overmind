<h1 align="center">Overmind</h1>

<p align="center"><strong>A meta-harness composition layer.</strong><br/>
It does not orchestrate agents. It orchestrates the orchestrators.</p>

---

## The thesis

Three projects in this space each solved one hard problem well and left the others broken. Nobody has composed them, because everyone wants to be the platform.

| Upstream | Solved brilliantly | Broken or missing |
|---|---|---|
| **[Ruflo](https://github.com/ruvnet/ruflo)** (66k★) | Persistent swarm memory: HNSW vector recall, embeddings, ReasoningBank, session state | Execution. An [independent audit of v3.5.51](https://github.com/ruvnet/ruflo/discussions/1513) found ~290 of 300+ MCP tools are JSON state records with **no execution backend** — `agent_spawn`, `task_assign`, `workflow_execute`, `neural_train` all stubs. ~10 tools actually execute |
| **[Omnigent](https://github.com/omnigent-ai/omnigent)** (7.8k★) | Real execution: multi-harness (Claude Code, Codex, Cursor, OpenCode, Pi), OS-level sandboxing via `bwrap`/`seatbelt`, three-tier policy engine, spend caps, cloud sandboxes | No planner. No shared memory across sessions. No consensus or verification layer. You supervise it manually |
| **[Open Multi-Agent](https://github.com/open-multi-agent/open-multi-agent)** (6.7k★) | Deterministic orchestration: runtime task-DAG planning, checkpoint/resume, execution receipts, replay, consensus, CI eval gates | Runs its own agents. Its sandbox story is thin next to Omnigent's, and it has no persistent cross-run memory |

Overmind is the wiring. **It contains no orchestration engine, no agent loop, no memory store, and no sandbox of its own.** Every one of those is an upstream dependency. What Overmind adds is the composition: routing, the adapter layer, and the gates that none of the three enforce.

```
                    ┌─────────────────────────────────┐
  goal ──────────►  │  OMA coordinator                │  plan
                    │  goal → task DAG at runtime     │
                    └───────────────┬─────────────────┘
                                    │
                    ┌───────────────▼─────────────────┐
                    │  OVERMIND                       │
                    │  · linearity gate (Cognition)   │  the only
                    │  · file-conflict serializer     │  code this
                    │  · cross-vendor review router   │  repo owns
                    │  · MAST gates (14 modes)        │
                    │  · receipts + replay ledger     │
                    └───────┬─────────────────┬───────┘
                            │                 │
          ┌─────────────────▼──────┐   ┌──────▼──────────────────┐
          │  Omnigent              │   │  Ruflo (memory only)    │
          │  sandboxed execution   │   │  memory_store/search    │
          │  policies, spend caps  │   │  embeddings_generate    │
          │  Claude/Codex/Cursor   │   │  HNSW recall            │
          └────────────────────────┘   └─────────────────────────┘
```

## The five design decisions

Each one comes from a specific published finding, not from taste.

### 1. Single-threaded by default; parallelism must be earned

Cognition's [*Don't Build Multi-Agents*](https://cognition.com/blog/dont-build-multi-agents) is right about the failure mode: parallel agents make **conflicting implicit decisions** about style, edge cases, and patterns, and integration cost exceeds the parallelism gain. Anthropic's [internal research](https://resources.anthropic.com/hubfs/Building%20Effective%20AI%20Agents-%20Architecture%20Patterns%20and%20Implementation%20Frameworks.pdf) reports the opposite result — multi-agent beating single-agent by 90.2% — but only for tasks requiring **genuinely independent** directions, at ~15x the token cost.

Both are true. The variable is independence, so Overmind measures it instead of guessing:

- The planner must declare `reads` and `writes` per task.
- Two tasks run in parallel **only** if their file sets are disjoint and neither depends on an unstated decision of the other.
- Any overlap collapses them into a sequential chain, so the second agent inherits the first's full context.
- Fan-out above `max_parallel` requires an explicit `--wide` flag. Cost is logged per node.

The default is a linear agent with full context. You pay for parallelism deliberately.

### 2. The reviewer is never the same vendor as the author

Stolen outright from Omnigent's `Polly` example agent, and it is the single highest-leverage trick in the ecosystem. A model reviewing its own output shares its blind spots. Overmind routes every diff to a reviewer on a **different vendor** than the one that wrote it — Claude writes, GPT reviews, or vice versa. Configured in `overmind.toml`, enforced in `router.py`, and a same-vendor pairing is a hard error rather than a warning.

### 3. Verification is a node type, not a prompt suffix

The [MAST taxonomy](https://arxiv.org/abs/2503.13657) (NeurIPS 2025, 14 failure modes from 200 traces across 7 frameworks, κ=0.88) puts **task verification** as one of three top-level failure categories, and finds 79% of multi-agent failures trace to specification and coordination rather than model capability.

So verification is not an instruction appended to a worker prompt. It is a distinct DAG node with its own budget, its own model, and a machine-checkable exit condition — tests pass, schema validates, build succeeds. `docs/MAST-GATES.md` maps all 14 modes to a concrete gate in `gates.py`. Unmapped modes are listed as unmapped rather than quietly ignored.

### 4. Memory is read from Ruflo, work is never dispatched to it

This is the sharpest engineering call in the repo. Ruflo's memory layer is genuinely good and genuinely executes: `memory_store`, `memory_search` over HNSW, `embeddings_generate`. Its orchestration tools mostly do not run anything.

Overmind therefore uses Ruflo as a **vector memory service and nothing else**. `memory.py` allowlists exactly four tool names; calling anything else raises. The allowlist is in code, not documentation, so it cannot rot:

```python
ALLOWED = frozenset({"memory_store", "memory_search", "embeddings_generate", "session_save"})
```

If upstream ships real backends for the rest, widening the allowlist is a one-line change with a test.

### 5. Nothing executes outside a sandbox and a budget

Every task node runs through Omnigent, which means `bwrap` on Linux and `seatbelt` on macOS, plus its three-level policy stack. Overmind ships a default policy set that asks before shell writes, caps tool calls per session, and enforces a hard USD ceiling per run. A run that would exceed its budget halts at the last checkpoint instead of failing halfway through a diff.

## What this repo actually contains

```
overmind/
├── router.py        vendor-diversity routing + harness selection
├── linearity.py     the independence analysis from decision 1
├── gates.py         MAST-derived gates
├── memory.py        Ruflo MCP client, allowlisted
├── executor.py      Omnigent dispatch
├── receipts.py      append-only run ledger, replayable
└── cli.py           overmind run "goal"
bridge/planner.ts    thin service exposing OMA's coordinator
agents/*.yaml        Omnigent agent definitions (declarative)
```

Roughly 900 lines of Python and 100 of TypeScript. It is small on purpose. Every line that could be an upstream dependency is one.

## Install

```bash
git clone https://github.com/ajaygm18/overmind && cd overmind
make setup     # installs omnigent, ruflo, the OMA bridge, and this package
overmind doctor
```

`make setup` installs upstreams from their own registries — `uv tool install omnigent`, `npx ruflo@latest`, `npm install @open-multi-agent/core`. Overmind vendors nothing and forks nothing, so upstream updates are `make update`.

## Use

```bash
# plan only — prints the DAG, the parallelism decisions, and the cost estimate
overmind plan "Add OAuth2 device flow to the auth service"

# plan, approve, execute
overmind run "Add OAuth2 device flow to the auth service" --budget 8.00

# replay a previous run from its receipts, no model calls
overmind replay <run-id>
```

## Honest limitations

- **Upstream churn is the main risk.** Ruflo shipped 1,488 releases and averages an alpha every few days. Adapters are pinned and contract-tested in CI; expect breakage anyway.
- **The linearity gate depends on the planner declaring file sets honestly.** If it under-declares `writes`, two agents will collide. `gates.py` catches this after the fact via receipts, not before.
- **Cross-vendor review needs two credentials.** With one vendor configured, Overmind refuses to run rather than silently degrading to same-vendor review.
- **This does not make agents smarter.** It removes coordination failures, which the MAST data says are 79% of the problem. The remaining 21% is model capability and no amount of orchestration touches it.

## License

Apache 2.0, matching Omnigent. Upstreams retain their own licenses: Ruflo and OMA are MIT, Omnigent is Apache 2.0.
