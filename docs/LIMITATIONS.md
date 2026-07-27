# Limitations

Known gaps, each with what would close it. Four are recorded as spec deviations
in [TASKS.md](TASKS.md); the rest are properties of the design.

This file exists because the alternative is a README that reads better than the
software behaves.

## Gates

**`decision_surface` and `action_trace` are detective, not preventive.** They read
a receipt after a node has finished, so the tokens are already spent and the diff
already written. They catch the failure before it reaches the base branch, which
is the point, but they do not stop it happening. The preventive half lives in the
Omnigent policies compiled by `policy_export.py` (T01), which run in-session —
and policies can only see tool calls, not intent. *Fix:* nothing clean. This is
the boundary between what a harness can enforce and what only a reader can
judge.

**`ambiguity_halt`'s threshold is uncalibrated.** `ambiguity_threshold = 0.65`
is a guess, and the score comes from the planning model's own self-report. A
model that is confidently wrong reports low ambiguity. *Fix:* a labelled set of
goals with known ambiguity, and a threshold chosen from it rather than picked.

**`loop_detect_semantic` degrades quietly in one direction.** With Ruflo
reachable it compares embeddings; without it, character n-grams. The fallback
catches rephrased repeats far less reliably. Every result names the measure used,
so `via ngram` should be read as the weaker check — but nothing forces the reader
to notice. *Fix:* fail the gate loudly when embeddings were expected and
unavailable, which needs a config flag distinguishing "memory optional" from
"memory required".

**`prove_disjoint` depends on the rewriter having populated `plan.levels`.** It
asks whether nodes that ran concurrently wrote to disjoint paths, and it learns
which nodes those were from `levels`. A plan reaching it with empty levels passes
trivially. In the CLI path the rewriter always fills them; in the T09 fixtures
the driver sets them explicitly for exactly this reason. *Fix:* make the gate
refuse an empty `levels` on a multi-node plan instead of passing.

**The first mis-declared write can still collide.** The rewriter schedules from
*declared* file sets, because the decision must be made before any code runs.
Actual writes are measured afterwards and `declared_scope` fails undeclared
paths, so a mis-declaration fails at its gates rather than shipping — but two
concurrent sessions can still have collided in the meantime. See the ADR-003
amendment. *Fix:* none available; this is inherent to scheduling before
execution.

## Observability

**Span durations are derived, not measured.** `Receipt` records `at`, the moment
the entry was appended, and has no start timestamp. `overmind export` therefore
runs each node's span from the previous ledger entry to its own. Every span
carries `overmind.timing.source="derived-from-ledger-order"` and the CLI repeats
the caveat, so the numbers are labelled rather than fabricated — but they are not
latency. *Fix:* add `started_at` to `Receipt` and set it in `executor.execute`.
Small change, deliberately not bundled into T10.

## Integration

**The rollback-failure path is untested.** `tests/test_integrate_git.py` covers a
real conflict, a real rollback, and the dirty-tree refusal against actual git
repositories. It does not cover `rollback()` itself failing mid-abort — the case
where a merge conflicts *and* the reset cannot complete, leaving the tree in a
state the code does not describe. T05's seventh subtask, not met. *Fix:* a
fixture that makes `git reset --hard` fail, which means an unwritable `.git` or
an injected failure at the subprocess boundary.

## Upstream coupling

**Worktree confinement does not use `block_working_dir_changes`.** Omnigent's
`POLICIES.md` names that builtin but does not give its dotted handler path, and
only three builtin paths are verbatim-confirmed. Emitting a guessed import path
would fail at session start, so confinement is enforced by an owned `dir_guard`
handler plus `sandbox.write_paths` (ADR-010). The behaviour is equivalent for the
cases that matter; the mechanism is ours, so upstream improvements to that
builtin are not inherited. T03's criterion "every generated spec contains
`block_working_dir_changes`" is **not** met. *Fix:* confirm the handler path from
upstream source and switch to it.

**Omnigent and Ruflo float.** Both are installed as latest (`uv tool install
omnigent`, `npx ruflo@latest`). Omnigent is alpha and pinning would mean running
an older sandbox on purpose; Ruflo ships faster than any pin would track. An
upstream change can therefore break a working checkout. `tests/contracts/`
asserts the exact surfaces this repo calls and separates *unreachable* (skip)
from *changed* (fail). *Fix:* pin once Omnigent is past alpha. See
[PINS.md](PINS.md).

**No lockfiles on either side.** Python dependencies are lower bounds with no
`uv.lock`; `bridge/` has no `package-lock.json`, so the Docker image is
repeatable, not reproducible. *Fix:* commit both, each of which needs a tool run.

**The upstream studies are documentation-derived.** `docs/upstream/` was written
from READMEs, specs, and design docs at the SHAs in PINS.md, not from running
every feature. Ruflo's stub inventory rests on a community audit plus one
maintainer-thread report (ADR-002), which is two independent negative signals and
not a measurement. *Fix:* the contract tests, which is why they run against real
CLIs rather than mocks.

## Scope

**One topology, and no cost model beyond a budget cap.** A DAG with compulsory
verification nodes, per ADR-006. Budget is enforced by summing receipts and
halting; there is no per-model pricing table, so `cost_usd` is only as good as
what each harness reports. *Fix for the second half:* a pricing table, which then
needs maintaining against provider changes.

**`plan --dry-run` does not validate a plan.** It prints the exact request the
bridge would receive and stops. There is no plan to validate until the
coordinator answers, so offline validation would have to validate a fabricated
one. *Fix:* none wanted; the field-level rejections are asserted in
`tests/test_bridge_contract.py`.
