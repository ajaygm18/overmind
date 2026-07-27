# Study: Open Multi-Agent (OMA)

**Repository:** `open-multi-agent/open-multi-agent` · TypeScript 5.6 · MIT · ~6.7k stars
**Pinned at:** `86919ae415bb599108b3180ba2227a01a04b7a73`
**Primary sources read:** `docs/task-scheduling.md`, `docs/` index, README
**Role in Overmind:** planning and DAG scheduling

---

## 1. Philosophy

OMA's documentation reads like a distributed-systems spec rather than an AI
framework tutorial, and that is the clearest signal of what it believes: **a
multi-agent run is a scheduling problem with a language model in the worker
slot.** The interesting failures are not prompt failures, they are coordination
failures — a task dispatched to an ineligible agent, a dependency payload that
silently degraded, a task reported as skipped while its worker kept running.

Three convictions show through the defaults:

**Fail before dispatch, not during.** The complete plan is validated against task
requirements *before any task runs*. An unassignable task raises
`INVALID_TASK_REQUIREMENTS` with a specific issue code rather than falling back
to a plausible-looking agent. The doc states the rule flatly: "hard requirements
never fall back to an ineligible agent."

**Never degrade silently.** When a task opts into `dependencyPayload:
'structured'` and the upstream structured value is missing or non-serializable,
the dependent task *fails with a machine-readable validation error*. The doc is
explicit: "OMA never silently falls back to raw output." This is the single most
transferable idea in the repository.

**Reported state must match real state.** Abort, budget exhaustion, and approval
rejection all share one **drain-then-skip** path, specifically so that no task is
ever "reported as skipped while its agent continues running." The correctness
property being protected is the honesty of the run record.

## 2. Architecture

```
runTeam() / runTasks() / runFromPlan() / restore()
            │  (all four share one scheduler)
            ▼
     ┌──────────────┐   task:ready    ┌───────────┐
     │  TaskQueue   │ ──────────────► │ Scheduler │
     │ deps, skip,  │                 │  assign   │
     │  cascade     │ ◄────────────── │ 1 task    │
     └──────────────┘   completion    └─────┬─────┘
                                            ▼
                                    ┌───────────────┐
                                    │ Dispatch gate │ cancellation
                                    │               │ budget
                                    │               │ approval
                                    └───────┬───────┘ pool capacity
                                            ▼
                                    ┌───────────────┐
                                    │   AgentPool   │ ◄── semaphore is the
                                    │  (workers)    │     concurrency authority
                                    └───────┬───────┘
                                            ▼
                              TraceStore · checkpoints · receipts
```

The load-bearing detail is that **`AgentPool`'s semaphore is the single
concurrency authority**, including for ephemeral `delegate_to_agent` runs. The
dispatch gate is described as "an integration seam; it does not add resource
locks or a second concurrency system." OMA deliberately refuses to have two
things that both think they control parallelism.

## 3. Modules

| Package / concept | Responsibility |
| --- | --- |
| `@open-multi-agent/core` | Orchestrator, scheduler, `TaskQueue`, `AgentPool`, checkpoints |
| `@open-multi-agent/otel` | OpenTelemetry spans, `TraceStore`, offline Run Viewer |
| `create-oma-app` | Project scaffolding |
| `createTeam` / `runTeam` | Roster definition and coordinator-planned execution |
| `runTasks` | Explicit task DAG supplied by the caller |
| `runFromPlan` / `restore` | Frozen-plan replay and checkpoint restore |
| `defineTool` | Typed tool declaration |
| `ContextStrategy` | Pluggable context-window management |

Documented subsystems worth knowing about: `adaptive-recovery`, `checkpoint`,
`consensus`, `context-management`, `evaluation` (EvalSets and CI gates),
`execution-routing`, `model-routing`, `plan-replay`, `shared-memory`,
`external-agents`, `tool-configuration`, and four separate observability
documents including `observability-release-readiness`.

### Identity model

OMA separates three things most frameworks conflate:

- **`assignee`** — the concrete worker instance (`supplier-reader-01`)
- **`role`** — the logical business function (`supplier-extraction`)
- **`metadata`** — bounded provenance (`sourceFile`, `supplierId`)

Receipts keep legacy `rolesExecuted` assignee semantics *and* add
`workerInstancesExecuted` plus `taskRolesExecuted`, "so worker replicas are not
confused with business roles." Metadata is hard-bounded: ≤16 entries, keys 1–64
chars beginning with a letter, values ≤1024 chars, arrays ≤16 values, the `oma.`
prefix reserved, credential-like keys rejected, and credential-like *values*
redacted before they reach results, spans, checkpoints, or plan artifacts.

## 4. Flow

1. **Plan validation.** The whole DAG is checked against `requires`
   (`requiredTools`, `requiredCapabilities`, `requiredBackend`,
   `requiredProvider`). Tool requirements are resolved *after* presets,
   allowlists, denylists, and framework rails; provider requirements *after*
   model routing, with incompatible fallback routes removed rather than crossing
   a declared provider boundary. Failures: `NO_ELIGIBLE_AGENT`,
   `ASSIGNEE_REQUIREMENTS_MISMATCH`. Coordinator plans naming an off-roster agent
   fail by default (`strictAssignees`).
2. **Ready emission.** `TaskQueue` emits `task:ready` when dependencies resolve.
3. **Assignment.** Every strategy filters to eligible agents first, then ranks:
   `dependency-first` and `composite` order by downstream criticality,
   `round-robin` keeps a cursor, `least-busy` reads `in_progress` load.
   `composite` maximizes `fitWeight * fit + loadWeight * (1 - normalizedLoad)`,
   defaulting to `0.7 / 0.3`.
4. **Dispatch gate.** Cancellation, budget, approval, pool capacity.
5. **Execution** through `AgentPool`.
6. **Completion** immediately unblocks dependents; a failure or skip cascades to
   dependents at once while unrelated branches continue.
7. **Checkpoint** per completion, serialized through one save chain. Restore does
   not rerun completed tasks.

Approval has two mutually exclusive modes: `onTaskDispatch` (per-task, fires
after assignment and immediately before dispatch) and `onApproval` (legacy
round-based barrier). Configuring both throws. There is deliberately no third
`legacyBatchScheduling` flag — the doc reasons that `onApproval` already *is* the
compatibility switch, and a second overlapping mode flag would be redundant.

## 5. What Overmind takes, and what it must not take

**Takes:** planning. OMA generates the initial task DAG via `createTeam` /
`runTeam` behind Overmind's `bridge/planner.ts` HTTP service. Overmind consumes
`result.agentResults.get('coordinator')` and normalises it into its own `Plan`.

**Takes as design, not as code:** four ideas are borrowed at the level of
principle and reimplemented in Python because they must operate on Overmind's own
plan objects.

- *Validate the whole plan before spending anything.* Overmind's `plan_gates()`
  runs before the first node executes, for the same reason.
- *Never degrade silently.* Overmind's cross-vendor router refuses to start
  rather than fall back to same-vendor review — the same rule applied to a
  different resource.
- *Reported state must match real state.* This is the ancestor of Overmind's
  receipt ledger and of the rule that a node which is `status="ok"` but carries a
  blocking gate is not counted as done.
- *One concurrency authority.* Overmind's rewriter is the only thing that decides
  width; the executor never opportunistically adds parallelism.

**Must not take:** OMA's execution path. OMA dispatches to LLM workers through
`AgentPool`; it is not git-aware and has no sandbox. Overmind's workers are
coding agents mutating a real worktree, which is a different resource model — its
safety argument is about file conflicts, not pool capacity. Using OMA's scheduler
for execution would mean adopting a concurrency authority that cannot see the
resource actually being contended.

**Must not take:** `consensus`. Multiple agents voting on an answer is not
verification. Overmind requires a machine-checkable exit condition instead.

**Contract surface to test:** `createTeam`, `runTeam`, and the shape of
`TeamRunResult.agentResults`. The `taskResults` map and `dependencyPayload` modes
are not currently used, but `taskResults` is the correct migration target if the
bridge ever needs per-task output rather than a single coordinator string.
