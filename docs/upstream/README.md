# Upstream studies

These documents exist because Overmind's central claim — that it composes rather
than reimplements — is only credible if the seams are understood in detail. A
composition layer built on a guess about what upstream does is worse than a
reimplementation, because the guess fails silently at integration time.

Each study is written from primary sources at a pinned commit, not from the
README and not from third-party summaries. Where a claim comes from a community
audit rather than code, it is labelled as such.

| Study | Upstream | Pinned at | Role in Overmind |
| --- | --- | --- | --- |
| [`oma.md`](oma.md) | `open-multi-agent/open-multi-agent` | `86919ae415bb599108b3180ba2227a01a04b7a73` | Planning and DAG scheduling |
| [`omnigent.md`](omnigent.md) | `omnigent-ai/omnigent` | `09a035ebb3765054f8312c0d5eba8a5e7f818d49` | Sandboxed execution and policy enforcement |
| [`ruflo.md`](ruflo.md) | `ruvnet/ruflo` | `e263c269dfb1eee948539c95121d1c816ca64629` | Vector memory across runs |
| [`COMPARISON.md`](COMPARISON.md) | all three | — | Seam analysis: overlaps, gaps, conflicts |

## How to read these

Each study answers the same five questions in the same order, so they can be
diffed against each other:

1. **Philosophy** — what the project believes about agents, stated or implied by
   its defaults.
2. **Architecture** — the major components and how they relate.
3. **Modules** — the concrete units, named as upstream names them.
4. **Flow** — what actually happens on a request, step by step.
5. **What Overmind takes, and what it must not take** — the integration contract.

The fifth section is the one that matters. A study that stops at admiration is
not useful; the point is to decide precisely where the boundary sits.

## A note on evidence quality

The three upstreams are not equally verifiable, and the studies do not pretend
otherwise.

- **OMA** documents behaviour precisely and in the language of failure modes
  (`INVALID_TASK_REQUIREMENTS`, `drain-then-skip`, "OMA never silently falls back
  to raw output"). Its docs read as specifications and can be relied on.
- **Omnigent** documents its interfaces well — the policy event shape and the
  agent YAML schema are stable enough to build adapters against — but it is
  self-described alpha and its harness matrix moves.
- **Ruflo** documents ambitiously and the documentation substantially overstates
  what executes. Its study is therefore the most sceptical of the three, and the
  integration contract derived from it is the narrowest.

This asymmetry is a design input, not a complaint. Overmind depends most heavily
on the upstream that specifies its behaviour most precisely, and depends on
Ruflo for exactly one capability that is independently confirmed to work.
