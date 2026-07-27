# Seam analysis

The three upstreams were not chosen because they are the three best agent
projects. They were chosen because **their strengths are disjoint and their
weaknesses do not overlap.** This document shows the arithmetic behind that
claim, and — more importantly — the capabilities that none of them provide, which
is the only honest justification for Overmind existing at all.

## 1. Capability matrix

Legend: ● strong · ◐ partial · ○ absent · ✗ present but rejected

| Capability | OMA | Omnigent | Ruflo | Overmind's source |
| --- | :-: | :-: | :-: | --- |
| Task decomposition / planning | ● | ○ | ✗ stub | **OMA** |
| DAG scheduling primitives | ● | ○ | ✗ stub | design only |
| Pre-dispatch plan validation | ● | ○ | ○ | **OMA** (as design) |
| Coding-agent harness adapters | ○ | ● | ○ | **Omnigent** |
| OS sandboxing | ○ | ● | ○ | **Omnigent** |
| Secretless credential handling | ○ | ● | ○ | **Omnigent** |
| In-session policy enforcement | ◐ | ● | ○ | **Omnigent** |
| Cost budgets | ◐ | ● | ○ | **Omnigent** + own |
| Human approval (ASK) | ● | ● | ○ | **Omnigent** |
| Vector memory (HNSW) | ◐ | ○ | ● | **Ruflo** |
| Embeddings | ○ | ○ | ● | **Ruflo** |
| Tracing / observability | ● | ◐ | ◐ | own ledger |
| Checkpoint + restore | ● | ◐ | ✗ stub | own (`resume.py`) |
| Plan freeze / replay | ● | ○ | ○ | own (`replay`) |
| Multi-agent voting consensus | ● | ○ | ✗ stub | ✗ **rejected** |
| Swarm topologies | ○ | ○ | ✗ stub | ✗ **rejected** |
| Agent marketplace / catalogue | ◐ | ◐ | ● | ✗ **rejected** |
| **Git worktree isolation per task** | ○ | ○ | ○ | **absent everywhere** |
| **Worktree reunification / merge** | ○ | ○ | ○ | **absent everywhere** |
| **Measured (not declared) file scope** | ○ | ○ | ○ | **absent everywhere** |
| **Mandatory cross-vendor review** | ○ | ◐ possible | ○ | **absent everywhere** |
| **Mandatory machine-checkable exit** | ◐ | ○ | ○ | **absent everywhere** |
| **Semantic loop detection** | ○ | ○ | ○ | **absent everywhere** |
| **Named failure-mode taxonomy gates** | ○ | ◐ policies | ○ | **absent everywhere** |

The bottom seven rows are the project. Everything above them is integration work.

## 2. Where they overlap, and who wins

Three genuine overlaps exist. Each is resolved by picking one owner outright,
because two systems that both believe they own a resource is the defect class
this whole repository is about.

**Concurrency control — OMA vs Overmind's rewriter.** OMA's `AgentPool` semaphore
is its "concurrency authority" and it explicitly refuses to let the dispatch gate
become a second one. Overmind must respect the same discipline, and cannot simply
adopt OMA's: `AgentPool` bounds *worker capacity*, while the contended resource in
Overmind is *files on disk*. Two nodes writing `src/auth.py` are unsafe at any
pool size. **Owner: Overmind's rewriter.** OMA is used for planning only, never
for dispatch, so the two authorities never coexist.

**Budgets — Omnigent `cost_budget` vs Overmind `distribute_budget`.** These
operate at different scopes and compose cleanly. Overmind divides the run budget
across nodes (1.0 producer / 0.33 inspector) and passes each node's share to
Omnigent as `max_cost_usd`. **Owner: Overmind allocates, Omnigent enforces.**
Omnigent's downgrade-gate semantics are strictly better than a hard stop at the
session level and are left intact.

**Approval — OMA `onTaskDispatch` vs Omnigent ASK.** OMA's approval hook is on a
dispatch path Overmind does not use. **Owner: Omnigent ASK**, which fires at the
actual dangerous moment (the tool call) rather than at task start.

## 3. Conflicts that must be resolved in code

| # | Conflict | Resolution |
| --- | --- | --- |
| C1 | Omnigent's `block_working_dir_changes` blocks `git worktree add` by default; Overmind's executor depends on it | Overmind creates worktrees **outside** the sandbox before the session starts. The policy is then *enabled* with `allowed_dirs` pinned to the node's worktree. The agent is confined to a worktree it did not create and cannot leave. |
| C2 | Omnigent "may append framework-owned instructions at runtime" — the prompt is not fully caller-controlled | Overmind never asserts prompt-level determinism. Determinism claims attach to the plan hash and receipts, which it does control. |
| C3 | Ruflo advertises orchestration that does not execute | Hardcoded four-name allowlist; `RufloToolNotAllowed` on anything else. |
| C4 | Ruflo memory can be unavailable, silently degrading gates | `MemoryUnavailable` + mandatory n-gram fallback; `Similarity.source` names the measure used. |
| C5 | Ruflo churn (1,488 releases / 10 months) | Pinned version + `upstream-contracts` CI job exercising the four tools. |
| C6 | OMA plan output is prose from a coordinator agent, not a typed DAG | `bridge/planner.ts` normalises; `linearity.validate` rejects malformed plans before any spend. |
| C7 | Two credentials are structurally required by mandatory cross-vendor review | Refuse to start with one. Silent same-vendor fallback would look identical in logs while providing far less. |

C1 is the one worth dwelling on. It was invisible from READMEs and only surfaced
by reading the builtin policy list — and resolving it correctly made the design
*stronger* than it was before, because the pre-existing executor had no answer to
"what stops the agent leaving its worktree?" The answer is now an upstream policy
Overmind opts into rather than around.

## 4. What no upstream provides

These are the capabilities Overmind must write, with the specific reason each is
absent rather than merely unimplemented.

1. **Per-task git worktree isolation, then reunification.** Omnigent isolates a
   *session* and never reunifies. OMA is not git-aware. Ruflo has no execution.
   Nobody merges, because nobody else treats a git branch as the unit of agent
   output.
2. **Measured file scope.** Every framework in the category takes the planner's
   declared file list as fact. Reading `git status` in the worktree and failing on
   undeclared paths requires knowing there *is* a worktree — which follows from
   (1) and therefore exists nowhere else.
3. **Retroactive disjointness proof.** The rewriter's concurrency decision rests
   on declarations made before execution. Re-proving it against measured writes
   after the level completes is only possible if (2) exists.
4. **Mandatory cross-vendor review.** Omnigent makes it *possible* — its own spec
   example is a `cursor` coder with a `claude-sdk` reviewer. Nobody makes it
   compulsory or refuses to start without it.
5. **Verification as a plan node type with a machine-checkable exit.** Frameworks
   offer reviewer agents whose output is an opinion. `ExitKind.MODEL_ASSERTION`
   exists in Overmind's enum but is excluded from `MACHINE_CHECKABLE` and can
   never be inherited by a synthesized verifier.
6. **Semantic loop detection.** Where loop detection exists at all it is
   step-count or exact repetition. Agents loop by *rephrasing*.
7. **Gates named after a published failure taxonomy.** Omnigent's policies gate
   *permissions* (may this tool run?). MAST's 14 modes describe *coordination*
   failures (did the agent drift from spec, drop context, skip verification?).
   Different axis, unoccupied.

## 5. Research inputs that shaped the design

**MAST — Cemri et al., NeurIPS 2025** ([arXiv:2503.13657](https://arxiv.org/abs/2503.13657)).
200 annotated traces from MetaGPT, ChatDev, HyperAgent, OpenManus, AppWorld,
Magentic, AG2; κ = 0.88; 14 failure modes in 3 categories. **79% of failures are
specification and coordination failures, not model-capability failures.** This is
the empirical case for a gate layer: most of what goes wrong is fixable by
orthogonal machinery. It also bounds the claim — orchestration fixes coordination,
not capability.

**Cognition, "Don't Build Multi-Agents"**
([cognition.com](https://cognition.com/blog/dont-build-multi-agents)). Parallel
subagents make *conflicting implicit decisions*. Overmind's serial-by-default
posture and the `prove_disjoint` gate are direct responses. The term "conflicting
implicit decisions" is Cognition's, and where it appears as a `mast_mode` string
it is flagged as **not** one of MAST's 14.

**Anthropic, multi-agent research system**
([anthropic.com](https://www.anthropic.com/engineering/building-effective-agents)).
90.2% improvement over single-agent on tasks with genuinely independent
directions, at roughly 15× tokens. Read together with Cognition, the two are not
contradictory — they describe different task shapes. Hence: parallelism is
available, must be *earned* by proving disjointness, and its cost is logged per
node so the 15× is visible rather than surprising.

**Omnigent's `Polly` example.** The origin of mandatory cross-vendor review. A
model that made an error is disproportionately likely to rate that error
acceptable; self-review shares blind spots.

**OMA's "never silently falls back".** The origin of Overmind's refuse-to-start
posture on single-vendor configuration, and of the rule that a node which is
`status="ok"` but carries a blocking gate does not count as done.
