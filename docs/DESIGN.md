# Overmind — derived design

This document is the synthesis of the three upstream studies in
[`docs/upstream/`](upstream/), the MAST failure taxonomy, and the published
disagreement between Cognition and Anthropic on multi-agent parallelism. It
defines what Overmind is, what it refuses to be, and how the pieces fit.

It supersedes the architecture sketch in `ARCHITECTURE.md` where the two differ.

---

## 1. Thesis

> Coding agents fail at coordination far more often than at coding. Coordination
> failures are cheap to detect mechanically and expensive to detect by reading
> transcripts. Therefore the highest-leverage layer is not a better agent — it is
> a **thin, opinionated, verifiable control plane over agents that already work.**

The empirical basis is MAST: 200 annotated traces across seven multi-agent
systems, and **79% of observed failures are specification or coordination
failures rather than model-capability failures**. If four in five failures are
fixable by machinery orthogonal to the model, that machinery is where the value
is. The same paper bounds the claim honestly: orchestration does not make a model
smarter.

Overmind therefore ships **no engine, no agent loop, no memory store, no
sandbox.** It ships judgement: what may run in parallel, who reviews whom, what
counts as done, what is recorded, and what happens on failure.

## 2. Design principles

Each principle has a named source and a named enforcement point. A principle with
no enforcement point is decoration.

| # | Principle | Source | Enforced by |
| --- | --- | --- | --- |
| P1 | Compose upstreams; write no engine | ADR-001 | dependency list; no vendored code |
| P2 | Validate the entire plan before spending anything | OMA | `plan_gates()` before node 1 |
| P3 | Never degrade silently — refuse instead | OMA | `router.audit` raises; `MemoryUnavailable` surfaces |
| P4 | Measure claims; never trust self-report | Ruflo audit | `introspect.actual_writes` from git |
| P5 | Parallelism must be earned, not assumed | Cognition + Anthropic | `linearity.rewrite` + `prove_disjoint` |
| P6 | Verification is a node, and its exit must be machine-checkable | MAST FM-3.3 | `MACHINE_CHECKABLE` frozenset |
| P7 | No two components may own the same resource | OMA `AgentPool` | rewriter is sole width authority |
| P8 | Enforce at the moment of the act, adjudicate after | Omnigent policies | policy export + receipt gates |
| P9 | The run record must match reality | OMA drain-then-skip | receipt ledger; gated-ok ≠ done |
| P10 | Least privilege by default | Omnigent | generated `os_env` + policy set |

## 3. Layered architecture

```
┌─────────────────────────────────────────────────────────────┐
│  L5  Interface        cli.py  ·  plan / run / resume / replay / list  │
│                              ·  doctor / gc                          │
├────────────────────────────────────────────────────────────┤
│  L4  Judgement    ── ORIGINAL CODE. This layer is the project. ──    │
│                                                                      │
│   linearity.py    rewrite the plan: serialize conflicts, cap width,   │
│                   interleave verifier nodes, repoint dependents       │
│   router.py       assign vendor/harness; refuse same-vendor review    │
│   gates.py        14 MAST-named coordination gates                    │
│   introspect.py   measure writes from git; re-prove disjointness      │
│   semantic.py     meaning-based loop + restatement detection          │
│   policy_export   compile gates into Omnigent policies  ← planned     │
│   integrate.py    reunify worktrees; conflict rollback                │
│   resume.py       plan snapshots; frontier recomputation              │
│   receipts.py     append-only JSONL ledger                            │
├────────────────────────────────────────────────────────────┤
│  L3  Adapters     executor.py (worktrees + omnigent run)              │
│                   memory.py (4-tool allowlist)                        │
│                   bridge/planner.ts (OMA over HTTP)                   │
├────────────────────────────────────────────────────────────┤
│  L2  Upstreams    OMA (plan)  ·  Omnigent (execute)  ·  Ruflo (recall) │
├────────────────────────────────────────────────────────────┤
│  L1  Substrate    git worktrees  ·  bwrap/seatbelt sandbox  ·  vendors │
└────────────────────────────────────────────────────────────┘
```

The only layer with novel intellectual content is **L4**. Layers 1–3 are
deliberately boring, and any cleverness there is a bug.

## 4. Control flow

```
overmind run "goal"

 1  config load + preflight        cfg valid, omnigent + git present,
                                   ≥2 vendor credentials — else refuse (P3)
 2  recall                         memory_search prior decisions/failures
 3  plan                           bridge → OMA coordinator → typed Plan
 4  validate                       _toposort; PlanInvalid on cycles
 5  rewrite                        ← THE decision point (P5, P7)
                                     · serialize write-conflicting nodes
                                     · cap width at max_parallel
                                     · synthesize verifier per producer
                                     · repoint dependents at the verifier
 6  plan gates                     ambiguity_halt, verify_required,
                                   cross_vendor_verify, explicit_exit (P2)
 7  snapshot                       save_plan(run_id) — refuses un-rewritten
 8  for each level, for each node in parallel:
        a  make_worktree           git worktree add, OUTSIDE the sandbox
        b  compile policies        gates → Omnigent policy set  ← planned
        c  write agent YAML        executor/os_env/tools/policies
        d  omnigent run            sandboxed, budgeted, policy-gated
        e  check_exit              machine-checkable proof (P6)
        f  audit_writes            git-measured scope (P4)
        g  node gates              exit_proof, decision_surface,
                                   action_trace, declared_scope,
                                   loop_detect_semantic
        h  receipt                 append-only (P9)
 9  prove_disjoint                 re-prove concurrency vs measured writes
10  lessons                        memory_store corrected paths
11  integrate                      sequential merge, conflict → rollback
12  summary                        cost by vendor, failed gates, plan hash
```

Step 5 is where the project earns its existence. Steps 3, 8d and 2 are upstream.

### Failure paths

| Failure | Behaviour |
| --- | --- |
| Ambiguous goal above threshold | halt before spending (`ambiguity_halt`) |
| One vendor credential | refuse to start (P3) |
| Cyclic or malformed plan | `PlanInvalid`, no spend |
| Node exceeds node budget | Omnigent downgrade-gate, then node failure |
| Node writes undeclared path | `declared_scope` fails → node blocking |
| Semantic loop detected | in-session DENY (planned) / post-hoc HALT (today) |
| Merge conflict at integration | `git merge --abort`, `reset --hard base_sha` |
| Rollback itself fails | report `LEFT DIRTY` — never claim clean |
| Memory unavailable | degrade: n-gram fallback, `source` reported |
| Interrupted run | `overmind resume <run-id>` from snapshot + receipts |

## 5. Gate placement model — the correction

The original implementation ran all 14 gates post-hoc against receipts. Reading
Omnigent's `POLICIES.md` showed that to be the wrong home for half of them.

A gate has a **decision time**. Post-hoc is correct only when the question cannot
be answered earlier.

| Gate | Question | Correct time |
| --- | --- | --- |
| `ambiguity_halt` | is the goal clear enough to spend on? | before planning |
| `verify_required`, `cross_vendor_verify`, `explicit_exit` | is the plan structurally sound? | before execution |
| `loop_detect_semantic` | is the agent stuck **right now**? | **in-session** |
| `declared_scope` | is this write outside declared scope? | **in-session** |
| `role_scope` | is this role doing another role's job? | **in-session** |
| `exit_proof` | did the machine-checkable check pass? | after the node |
| `action_trace`, `decision_surface` | is the record complete? | after the node |
| `prove_disjoint` | was the concurrency decision sound? | after the level |
| `clean_merge` | did the tree land intact? | after integration |

An in-session gate run post-hoc still detects the failure — but only after the
budget is spent and the bad write is on disk. The fix is a **compiler**:
`policy_export.py` turns Overmind gate configuration into Omnigent policy
declarations that DENY at the offending tool call, while the receipt gates remain
as defence in depth for harnesses that under-report tool calls. Two independent
detectors for the same fault is not redundancy — the failure modes differ.

## 6. Security model

Defence in depth, with each layer named and each one owned by someone specific.

1. **Process isolation** — Omnigent sandbox (`linux_bwrap` / `darwin_seatbelt`).
   Never `type: none` outside local dev. *Upstream.*
2. **Filesystem isolation** — `write_paths` pinned to the node's worktree.
   *Overmind generates, upstream enforces.*
3. **Directory confinement** — `block_working_dir_changes` **enabled** with
   `allowed_dirs` = the node worktree. The worktree is created *outside* the
   sandbox before the session starts, so the agent never needs, and never gets,
   `git worktree`. This resolves conflict C1 in the seam analysis by opting *into*
   the policy rather than around it. *Overmind's answer to a question its first
   design could not answer.*
4. **Network egress** — explicit `egress_rules`; no implicit widening.
5. **Credential isolation** — Omnigent `credential_proxy` where applicable; the
   sandbox holds placeholders, never live tokens.
6. **Tool allowlisting** — Ruflo restricted to four names;
   `terminal_execute` excluded despite working.
7. **Spend limits** — per-node `max_cost_usd` derived from the run budget.
8. **Receipt redaction** — receipts gitignored by default; `redact_tool_args`
   opt-in for shared environments.

## 7. Data contracts

Three contracts must stay stable, and each has an owner and a test.

**Plan** (`models.Plan`) — goal, nodes, levels, `rewritten`, `content_hash()`.
The hash is sha256[:16] over goal + levels + sorted nodes, and is what receipts
and snapshots reference. Divergence raises `PlanDiverged`.

**Receipt** (`models.Receipt`) — append-only JSONL, one per node/gate/run. Carries
`plan_hash`, vendor, harness, tokens, cost, tool calls, decisions, diff stat,
worktree, gate results, status. `replay` reconstructs a run from receipts with no
model calls, which is the property that makes post-hoc gates and CI evaluation
possible.

**Agent YAML** — generated per node, conforming to Omnigent's
`AGENT_YAML_SPEC.md`: `name`, `instructions`, `executor.{harness,model,auth}`,
`os_env.sandbox.{write_paths,egress_rules}`, `policies`, `tools`. This is the
widest upstream surface Overmind depends on and therefore needs a contract test.

## 8. Non-goals

Stated so that scope creep is visible when it happens.

- No orchestration engine, agent loop, memory store, or sandbox (P1).
- No web UI — Omnigent's is better than this repo would produce.
- No agent marketplace. Five well-scoped roles with compulsory verification beats
  100 roles without it.
- No swarm topologies. One topology: a DAG with verification nodes.
- No voting consensus. Agreement is not verification; a machine-checkable exit is.
- No prompt-level determinism claims — Omnigent may append framework instructions
  at runtime (conflict C2). Determinism attaches to plan hashes and receipts.
- No fine-tuning, no model training, no evaluation of model quality. Overmind
  measures coordination, not capability — the boundary MAST draws.

## 9. What "finished" means

The project is complete when all twelve tasks in [`TASKS.md`](TASKS.md) meet their
acceptance criteria. The headline criteria:

- Every gate with an in-session decision time is enforced in-session.
- Every upstream dependency has a contract test that fails loudly on breakage.
- Every git-touching code path has a test using a real temporary repository.
- A seeded MAST-mode failure is caught by the gate that names it — measured, not
  asserted.
- `README` quickstart runs end to end on a clean machine.
