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

**Consequences.** Slower than swarm-first tools on genuinely parallel work.

**Amendment (see ADR-008).** The original text of this ADR ended: *"the safeguard depends on the planner declaring file sets honestly. Under-declaration is caught after the fact by receipts, not prevented."* That was a real hole and it is now partly closed. Declared sets are still what the rewriter schedules from — that cannot change, since the decision must be made before any code runs — but they are no longer the last word. Each node's actual writes are read out of git, `declared_scope` fails any undeclared path, and `prove_disjoint` re-asks the disjointness question of the measured sets once the level has finished. The residual risk is now narrow and specific: the *first* run that mis-declares a path can still produce two concurrent sessions that collide. That run fails at its gates rather than shipping, and the corrected path is written to memory so the next plan declares it.

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

---

## ADR-007: The run integrates its own worktrees

**Status:** accepted

**Context.** The first cut ran each node in its own `git worktree` on its own branch and stopped there. Gates passed, receipts were written, the run reported success — and the base branch was untouched. The parallelism was decorative, and the isolation that made it safe also made it useless.

No upstream fills this in. Omnigent isolates sessions and does not reunify them. OMA schedules tasks and is not git-aware. Ruflo has no execution to integrate. This is composition-layer work by elimination, so it is written here.

**Decision.** A successful run merges its writing nodes' branches into the base branch itself, in `integrate.py`. Two rules make it trustworthy: merges are **sequential in plan order**, never an octopus merge, so a genuine collision surfaces as a conflict at a known point instead of interleaving two agents' choices; and the base SHA is recorded up front so any conflict aborts the merge and resets the branch. Integration refuses to start on a dirty tree, because rollback would otherwise discard the operator's uncommitted work.

**Consequences.** A run either lands completely or not at all. The cost is that a conflict wastes the whole level's work rather than half-landing it, which is the correct trade: a tree containing part of a plan is harder to reason about than one containing none of it.

---

## ADR-008: Measure what the plan asserts

**Status:** accepted

**Context.** Two of this repo's original gates trusted a language model's own account of its behaviour. `writes` was a promise made by the planner, and `loop_detect` compared tool calls by string equality. Both are the same mistake in different clothes: taking a model's description of what happened as evidence of what happened.

The string-equality one was the clearer failure. Agents rarely loop by issuing byte-identical calls; they loop by rephrasing. `grep "device_code"`, then `grep "device code"`, then `grep "device-code"` is a stuck agent burning budget, and hash comparison sees three distinct productive actions.

**Decision.** Where a claim can be measured, measure it. Writes come from `git status`/`git diff` in the node's worktree (`introspect.py`), not from the plan. Repetition is compared by embedding similarity (`semantic.py`) via Ruflo's `embeddings_generate` — one of the ~10 tools the audit in ADR-002 found actually works — with a character-n-gram fallback so the gate still runs with no model, no network, and no upstream process.

**Consequences.** `declared_scope` cannot be defeated by a harness that under-reports its tool calls, which is the specific weakness of the older `action_trace`; both are kept because they fail independently. The embedding path adds a memory dependency to a gate, so the fallback is mandatory and every result names the measure it used — `via ngram` should be read as the weaker check rather than silently trusted as the stronger one.

---

## ADR-009: A gate runs where its evidence is

**Status:** accepted

**Context.** All fourteen gates originally ran post-hoc, against receipts, after a node had finished. For several of them that is the wrong place and the right one already existed: Omnigent's `POLICIES.md` describes an in-session policy layer that sees every tool call before it executes, with ALLOW / DENY / ASK verdicts. A gate that can decide from a single tool call has no reason to wait for the node to end — waiting means the tokens are spent and the diff is written before anything objects.

**Decision.** Placement follows the evidence, not the gate list.

- **Decidable from one tool call → in-session policy.** `policy_export.compile_policies` emits `overmind_worktree_confinement`, `overmind_role_boundary`, `overmind_declared_scope`, `overmind_loop_guard`, and `overmind_tool_call_limit`, plus a conditional `overmind_node_budget`. A write outside the declared set is denied at the call, not reported afterwards.
- **Needs the whole node → receipt gate.** `exit_proof`, `decision_surface`, `action_trace`, `acceptance_drift`, `spec_conformance`, `review_ack`, `checkpoint_continuity`. None of these can be judged from an individual call; they are questions about what the node did in total, or about whether its account of itself holds together.
- **Needs the whole plan → plan gate.** `explicit_exit`, `verify_required`, `cross_vendor_verify`, `context_carry`, `ambiguity_halt`. These run before anything is spent, which is the cheapest possible place for a plan-shaped failure to be caught.

**Three gates deliberately run in both places.** `declared_scope`, `role_scope`, and loop detection exist as a policy *and* as a receipt gate. They are not redundant, because they fail for different reasons: a harness that under-reports its tool calls defeats the receipt gate, and `git status` in the worktree does not care what the harness reported. Defence in depth here costs one function call per node.

**Consequences.** The failure surfaces earlier and cheaper for the preventive half, and the composition layer now depends on the policy handler contract as well as the CLI surface — a second upstream coupling, covered by `tests/contracts/test_omnigent.py`. Policies can only see tool calls, so nothing about intent moved: `decision_surface` and `action_trace` remain detective by nature, recorded in `LIMITATIONS.md` rather than presented as prevention.

---

## ADR-010: Confine each session to its worktree using upstream's own policy layer

**Status:** accepted

**Context.** Each node runs in its own `git worktree` (ADR-007), which is what makes concurrent nodes safe to run and safe to abandon. Omnigent ships a builtin policy, `block_working_dir_changes`, whose documented defaults include `block_worktree: true` and `block_cd: true`, and which parses chained and wrapped shell commands. Its defaults are therefore incompatible with an executor whose entire isolation model is `git worktree add` — conflict C1 in `docs/upstream/COMPARISON.md`.

The easy resolution is to switch the policy off. That trades the isolation guarantee for the mechanism that was supposed to enforce it: an agent free to `cd` out of its worktree can write to another node's tree, or to the base branch, and the receipt gates would only notice afterwards.

**Decision.** Opt *into* confinement, and remove the reason the policy would fire.

1. The worktree is created **host-side, by the orchestrator, before the session exists**. The agent never runs a worktree command, so blocking worktree commands costs nothing.
2. `os_env.sandbox.write_paths` is pinned to that worktree, so the sandbox refuses writes outside it at the OS level.
3. `dir_guard.allowed_dirs` is pinned to the same path, so directory escapes are denied at the tool-call level as well.
4. The generated spec and instructions live in `.overmind/specs/<run_id>/`, outside the worktree and referenced by absolute path. Writing them inside would make the orchestrator's own bookkeeping an undeclared write in the node's scope, failing that node's `declared_scope` gate on its first tool call.

**`block_working_dir_changes` itself is not emitted.** `POLICIES.md` names it without giving a dotted handler path, and only three builtin handler paths are verbatim-confirmed from upstream docs. Emitting a guessed import path would fail at session start, so the equivalent behaviour is enforced by an owned `dir_guard` handler. T03's acceptance criterion "every generated spec contains `block_working_dir_changes`" is therefore **not met**, and is recorded as unmet in `TASKS.md` and `LIMITATIONS.md` rather than described as satisfied.

**Consequences.** Two independent layers stop an escape — OS sandbox and tool-call policy — and both are configured from one place, `agentspec.build_os_env`. The cost is that the confinement handler is ours: improvements upstream makes to its builtin are not inherited until the handler path is confirmed and the switch is made.
