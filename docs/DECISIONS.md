# Architectural decision records

## ADR-001: Compose upstreams; write no engine

**Status:** accepted

**Context.** The obvious build is another orchestration framework. The category already has dozens, several with real funding and thousands of contributors. Ruflo alone has 5,800+ commits.

**Decision.** Overmind ships zero orchestration engine, zero agent loop, zero memory store, zero sandbox. Each is an upstream dependency, installed from its own registry, unforked and unvendored.

**Consequences.** Overmind gets Omnigent's sandbox hardening and OMA's scheduler for free, including future fixes. It also inherits upstream churn and cannot fix upstream bugs without a fork. Adapters are pinned and contract-tested to make breakage loud.

**Rejected alternative.** Forking Ruflo and replacing its stubbed execution layer. Tempting — it has the largest community and the best memory — but it means owning a 5,800-commit codebase to fix a layer Omnigent already implements properly.

---

## ADR-002: Ruflo is a memory service, never an executor

**Status:** accepted

**Context.** An [independent audit of Ruflo v3.5.51](https://github.com/ruvnet/ruflo/discussions/1513) tested every major tool category and found ~10 of 300+ MCP tools actually execute. The rest record JSON state. Confirmed working: `memory_store`/`memory_search` (HNSW), `embeddings_generate`, `terminal_execute`, `session_save`. Stubbed: `agent_spawn`, `task_assign`, `neural_train`, `workflow_execute`, `wasm_agent_prompt`.

The audit is a community post, not a peer-reviewed artifact, and upstream disputes framing. Independently, a [maintainer-thread user report](https://github.com/ruvnet/ruflo/discussions/1666) describes no measurable improvement in practice. Two independent negative signals against marketing claims is enough to design defensively.

**Decision.** `memory.py` holds a hardcoded four-name allowlist. Any other tool name raises `RufloToolNotAllowed`. `terminal_execute` is deliberately excluded despite working, because Omnigent's sandboxed execution is strictly better.

**Consequences.** Overmind uses maybe 1% of Ruflo's surface area and gets the 1% that works. Widening the allowlist is one line plus a test if upstream ships real backends.

---

## ADR-003: Parallelism is opt-in and must be justified

**Status:** accepted

**Context.** Cognition argues against multi-agent parallelism on the grounds of conflicting implicit decisions. Anthropic reports 90.2% improvement from multi-agent on tasks with independent directions, at ~15x tokens. The published positions look contradictory and are not: they describe different task shapes.

**Decision.** Serial by default. Parallel only when declared file sets are disjoint. Fan-out beyond `max_parallel` needs `--wide`. Cost logged per node so the 15x is visible rather than surprising.

**Consequences.** Slower than swarm-first tools on genuinely parallel work, and the safeguard depends on the planner declaring file sets honestly. Under-declaration is caught after the fact by receipts, not prevented.

---

## ADR-004: Cross-vendor review is mandatory, not configurable

**Status:** accepted

**Context.** Borrowed from Omnigent's `Polly` agent, which routes each diff to a reviewer from a different vendor than the author. Self-review shares blind spots; a model that made an error is disproportionately likely to rate that error acceptable.

**Decision.** Author vendor ≠ reviewer vendor, enforced in `router.py`. With one vendor credential configured, `overmind run` refuses to start.

**Consequences.** Two credentials required. Refusing beats silently degrading to same-vendor review, which would look identical in logs while providing far less.

---

## ADR-005: Receipts are the source of truth, not logs

**Status:** accepted

**Context.** Debugging multi-agent failures from prose logs does not scale — the MAST authors annotated traces averaging 15,000 lines each.

**Decision.** Append-only JSONL ledger per run. Every node emits a structured receipt: plan hash, node id, harness, vendor, tokens, cost, tool calls, decisions, gate results, exit status. `overmind replay` reconstructs a run from receipts with no model calls.

**Consequences.** Post-hoc gates (`decision_surface`, `action_trace`) and CI eval gates both become possible because the run is queryable data. Receipts can contain sensitive tool arguments, so they are gitignored by default and redaction is opt-in via `overmind.toml`.

---

## ADR-006: No web UI, no agent marketplace, no swarm topologies

**Status:** accepted

**Rationale.** Omnigent's UI is already multi-device and better than what this repo would produce. Large agent catalogues mostly encode prompt variation — five well-scoped roles with mandatory verification beats 100 roles without it. Swarm topologies (queen/mesh/hierarchical) are the least evidence-backed idea in the category; Overmind has one topology, a DAG with compulsory verification nodes.
