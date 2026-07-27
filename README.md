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

So verification is not an instruction appended to a worker prompt. It is a distinct DAG node with its own budget, its own model, and a machine-checkable exit condition — tests pass, schema validates, build succeeds. [`docs/MAST-GATES.md`](docs/MAST-GATES.md) maps all 14 modes, plus two failures MAST does not name, to a concrete gate.

That mapping is **measured, not asserted**. [`docs/MAST-COVERAGE.md`](docs/MAST-COVERAGE.md) is generated from the traces in `tests/mast/`, and every trace is run through the real gate. Each mode needs a trace the gate blocks *and* a trace it lets through — a gate that rejects everything covers nothing — and a mode added to the table without a trace fails CI.

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
├── models.py         plan + receipt shapes; the contract between every layer
├── config.py         overmind.toml, including the vendor-diversity requirement
├── planner.py        bridge client; rejects an invalid plan by field path
├── linearity.py      the independence analysis from decision 1
├── router.py         vendor-diversity routing + harness selection + budgets
├── agentspec.py      generates Omnigent agent YAML per node
├── policy_export.py  compiles gates into in-session Omnigent policies
├── policies/         the policy handlers those specs point at
├── executor.py       Omnigent dispatch, one worktree per node
├── introspect.py     reads actual writes out of git; re-proves disjointness
├── semantic.py       embedding/n-gram similarity for loop + drift detection
├── gates.py          MAST-derived gates
├── integrate.py      merges the run's branches, or rolls the base back
├── resume.py         checkpoint reconstruction for a halted run
├── receipts.py       append-only run ledger, replayable
├── memory.py         Ruflo MCP client, allowlisted
├── otel.py           receipts → OTLP spans
└── cli.py            plan / run / resume / replay / export / doctor / version
bridge/planner.ts     thin service exposing OMA's coordinator
bridge/schema.ts      plan validation at the boundary; no silent repair
agents/*.yaml         Omnigent agent definitions (declarative)
tests/mast/           failure-mode traces behind docs/MAST-COVERAGE.md
```

Still small on purpose: every line that could be an upstream dependency is one. There is no orchestration engine, agent loop, memory store, or sandbox in here. What there is, is the composition and the gates — and the gates are where most of the code went, because none of the three upstreams enforces them.

Design and delivery record: [`docs/DESIGN.md`](docs/DESIGN.md), [`docs/TASKS.md`](docs/TASKS.md), [`docs/DECISIONS.md`](docs/DECISIONS.md) (ADR-001 … ADR-010), and the per-upstream studies in [`docs/upstream/`](docs/upstream/).

## Install

```bash
git clone https://github.com/ajaygm18/overmind && cd overmind
make setup     # installs omnigent, ruflo, the OMA bridge, and this package
overmind doctor
```

`make setup` installs upstreams from their own registries — `uv tool install omnigent`, `npx ruflo@latest`, `npm install @open-multi-agent/core`. Overmind vendors nothing and forks nothing, so upstream updates are `make update`.

Prefer containers for the bridge:

```bash
cp .env.example .env     # add the provider key that plans
make dev-up              # bridge on 7801, Jaeger on 16686
```

Omnigent and Ruflo deliberately stay on the host — one needs the real worktree and your credentials, the other is a stdio subprocess. [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) explains what runs where and why.

## Use

```bash
# free: shows the exact request the coordinator would receive, no model call
overmind plan "Add OAuth2 device flow to the auth service" --dry-run

# plan only — prints the DAG, the parallelism decisions, and the gate results
overmind plan "Add OAuth2 device flow to the auth service"

# plan, gate, approve, execute, integrate
overmind run "Add OAuth2 device flow to the auth service" --budget 8.00

# continue a halted run from its last good checkpoint
overmind resume <run-id>

# replay a previous run from its receipts, no model calls
overmind replay <run-id>

# send the run to any OTLP backend as one trace per run, one span per node
overmind export <run-id> --otlp http://127.0.0.1:4318

# what is actually installed, since the upstreams float on purpose
overmind version
```

## Honest limitations

The full list, each entry with what would fix it, is in [`docs/LIMITATIONS.md`](docs/LIMITATIONS.md). The ones worth knowing before you clone:

- **Upstream churn is the main risk, and the upstreams are not pinned.** Omnigent installs as latest and Ruflo runs via `npx ruflo@latest`, deliberately — Omnigent is alpha and pinning would mean running an older sandbox on purpose. Neither side has a lockfile. What exists instead is a blocking contract-test job that asserts the exact surfaces this repo calls and distinguishes *unreachable* (skip) from *changed* (fail). See [`docs/PINS.md`](docs/PINS.md).
- **Parallelism is scheduled from declared file sets, then checked against measured ones.** The rewriter has to decide before any code runs, so it uses the planner's declared `writes`. Afterwards the real writes are read out of git: `declared_scope` fails any undeclared path, and `prove_disjoint` re-asks the disjointness question of the measured sets. The residual risk is narrow and real — the *first* run that mis-declares a path can still have two sessions collide. That run fails at its gates instead of shipping, and the corrected path goes to memory.
- **Some gates are detective, not preventive.** Whatever can be decided from a single tool call runs as an in-session Omnigent policy and is denied before it happens (ADR-009). `decision_surface` and `action_trace` need the whole node, so they catch a failure before it reaches the base branch but after the tokens are spent.
- **Span durations from `overmind export` are derived, not measured.** A receipt records when it was written and has no start timestamp, so a node's span runs from the previous ledger entry to its own. Every span says so via `overmind.timing.source`. Read the shape and the costs; do not read the milliseconds as latency.
- **Cross-vendor review needs two credentials.** With one vendor configured, Overmind refuses to run rather than silently degrading to same-vendor review.
- **This does not make agents smarter.** It removes coordination failures, which the MAST data says are 79% of the problem. The remaining 21% is model capability and no amount of orchestration touches it.

## License

Apache 2.0, matching Omnigent. Upstreams retain their own licenses: Ruflo and OMA are MIT, Omnigent is Apache 2.0.
