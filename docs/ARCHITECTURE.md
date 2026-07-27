# Architecture

## Layer boundaries

Overmind is one layer with four adapters. The rule that keeps it small: **if a behaviour exists upstream, Overmind calls it rather than owning it.**

| Layer | Owner | Overmind's role |
|---|---|---|
| Planning | OMA coordinator | Sends the goal, receives a task DAG, rewrites it (see below) |
| Independence analysis | **Overmind** | Nobody upstream does this |
| Routing | **Overmind** | Vendor diversity, harness selection |
| Execution | Omnigent | Dispatch, collect, attribute cost |
| Sandboxing | Omnigent (`bwrap`/`seatbelt`) | Pass-through, never bypassed |
| Policy | Omnigent | Ships defaults, does not reimplement the engine |
| Memory | Ruflo (HNSW) | Allowlisted client |
| Receipts | OMA + **Overmind** | OMA per-run; Overmind maintains the cross-run ledger |
| Verification | **Overmind** | Gate nodes with machine-checkable exits |

## The plan-rewrite step

OMA returns a DAG. Overmind does not execute it as given. It rewrites it, and the rewrite is where the value is.

```
OMA DAG ──► 1. annotate     attach reads/writes to every node
            2. serialize    collapse write-conflicting siblings into chains
            3. interleave   insert a verify node after every write node
            4. route        assign harness + vendor; enforce reviewer diversity
            5. budget       distribute the run budget across nodes; halt-not-fail
            ──► executable plan, frozen and hashed
```

The frozen plan is content-hashed. Receipts reference the hash, so a replay that produces a different plan is detected rather than silently diverging. This is OMA's plan-freeze idea applied to a rewritten plan.

### Why serialize instead of merge-resolving

When two tasks write the same file, the tempting fix is to let both run and reconcile the diffs. That is exactly the integration problem Cognition describes: the conflict is rarely textual, it is a **decision** conflict, and a merge tool cannot see it. Serializing costs wall-clock time and saves the class of failure that produces plausible-looking, subtly incoherent code.

## Execution model

One task node maps to one Omnigent session. Overmind never runs two sessions against the same worktree.

```
node ──► git worktree add (isolated)
     ──► omnigent run agents/<role>.yaml --harness <assigned>
     ──► capture: diff, tokens, cost, tool calls, exit status
     ──► verify node (different model, machine-checkable exit)
     ──► review node (different vendor, mandatory)
     ──► receipt appended, worktree retained until run ends
```

Worktrees are retained on failure so a failed node is inspectable. `overmind gc` removes them.

## Memory contract

Overmind writes to Ruflo memory at two points only, so recall stays high-signal:

1. **After a verified node** — the decision made, not the diff. "Chose device-code polling over websocket callback because the CLI has no listener."
2. **After a failed gate** — the failure mode and its MAST classification, so the next planner sees the prior failure.

Reads happen once, during planning, and get injected into the coordinator prompt as prior decisions. This is deliberately narrow. Storing whole transcripts is what makes vector memory return noise.

## Failure and resume

All durability is OMA's checkpointing plus the Overmind receipt ledger. There is no bespoke state machine.

- Node fails → receipt records it, run halts at that node, worktree kept.
- `overmind resume <run-id>` restarts from the last checkpoint with the failure and its MAST class injected into context.
- Budget exhausted → treated as a halt, not a failure. Checkpoint is clean and resumable with a raised budget.

## What is deliberately absent

- **No web UI.** Omnigent already ships one, including mobile, and it is better than anything this repo would build.
- **No agent marketplace.** `agents/` holds five roles. Ruflo's 100+ agent catalogue mostly encodes prompt differences; five well-scoped roles plus vendor diversity outperforms a large catalogue with no verification.
- **No swarm topologies.** Queen/mesh/hierarchical topologies are the part of the ecosystem with the least evidence behind it. Overmind has one topology: a DAG with mandatory verification.
- **No neural training / self-learning.** The upstream tools for this are the audited stubs. Memory recall is the honest version of the same idea.
