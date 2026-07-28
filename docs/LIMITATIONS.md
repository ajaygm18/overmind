# Limitations

Known gaps, each with what would close it.

This file exists because the alternative is a README that reads better than the
software behaves. It is rewritten when something closes, so a gap listed here is
a gap that is open today. Entries closed since the twelve tasks finished are at
the bottom, with what replaced them, because a reader who saw the earlier
version deserves to be told which items moved.

## Gates

**`decision_surface` and `action_trace` are detective, not preventive.** They read
a receipt after a node has finished, so the tokens are already spent and the diff
already written. They catch the failure before it reaches the base branch, which
is the point, but they do not stop it happening. The preventive half lives in the
Omnigent policies compiled by `policy_export.py` (T01), which run in-session —
and policies can only see tool calls, not intent. *Fix:* nothing clean. This is
the boundary between what a harness can enforce and what only a reader can
judge.

**`ambiguity_halt`'s threshold is still provisional.** Half of this is now
closed: `overmind/calibration.py` defines the labelled corpus format and the
chooser that maximises Youden's J over recorded scores, `tests/mast/ambiguity.jsonl`
ships twenty labelled goals with a stated reason each, and
`tools/calibrate_ambiguity.py` exits non-zero rather than printing a number when
the corpus has no scores. What is missing is the scores themselves: the input to
this gate is the planning model's self-report, so they can only be recorded
against a live coordinator, one goal at a time. Until that run happens
`ambiguity_threshold = 0.65` is a guess, labelled as one in the config file, in
the gate's docstring, and in `PROVISIONAL_DEFAULT`. A test asserts the corpus is
still unscored, so the day it stops being true, CI says so. *Fix:* record the
scores and re-run the chooser.

**A confidently wrong planner reports low ambiguity.** No threshold helps with
this, calibrated or not. The score is a self-report, and a model that has
misunderstood a goal is not usually uncertain about it. *Fix:* none available
without a second, independent ambiguity judgement, which is a different design.

**The first mis-declared write can still collide.** The rewriter schedules from
*declared* file sets, because the decision must be made before any code runs.
Actual writes are measured afterwards and `declared_scope` fails undeclared
paths, so a mis-declaration fails at its gates rather than shipping — but two
concurrent sessions can still have collided in the meantime. See the ADR-003
amendment. *Fix:* none available; this is inherent to scheduling before
execution.

## Upstream coupling

**Worktree confinement does not use `block_working_dir_changes`.** Omnigent's
`POLICIES.md` names that builtin but does not give its dotted handler path, and
only three builtin paths are verbatim-confirmed. Emitting a guessed import path
would fail at session start, so confinement is enforced by an owned `dir_guard`
handler plus `sandbox.write_paths` (ADR-010). The behaviour is equivalent for the
cases that matter; the mechanism is ours, so upstream improvements to that
builtin are not inherited. T03's criterion "every generated spec contains
`block_working_dir_changes`" is **not** met. *Fix:* confirm the handler path from
upstream source and switch to it. Guessing it is worse than the gap.

**Omnigent and Ruflo float.** Both are installed as latest (`uv tool install
omnigent`, `npx ruflo@latest`). Omnigent is alpha and pinning would mean running
an older sandbox on purpose; Ruflo ships faster than any pin would track. An
upstream change can therefore break a working checkout. `tests/contracts/`
asserts the exact surfaces this repo calls and separates *unreachable* (skip)
from *changed* (fail). *Fix:* pin once Omnigent is past alpha. See
[PINS.md](PINS.md).

**No lockfiles on either side.** Every Python requirement is now bounded at the
next major and every bridge dependency is an exact version, so an unrelated
release can no longer walk into a checkout — but there is still no `uv.lock` and
no `package-lock.json`, which means transitive dependencies remain unpinned and
the Docker image is repeatable, not reproducible. Generating either needs a
resolver run, which is not something this repo's tooling performs. *Fix:* commit
both, from a real `uv lock` and `npm install`.

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

## Closed

Listed because they appeared here until recently, and because the replacement
behaviour is the part worth knowing.

**Span durations were derived, not measured.** `Receipt` now carries
`started_at`, set in `executor.execute` before the worktree is created.
`overmind export` uses it when present and marks those spans
`overmind.timing.source="measured-from-receipt"`; the run span claims to be
measured only when every node in the ledger is. Ledgers written before this
change still export, still say `derived-from-ledger-order`, and
`Receipt.duration_s` returns `None` rather than `0.0` when nothing was measured.

**`loop_detect_semantic` degraded quietly.** `[memory] require_embeddings`
distinguishes "memory optional" from "memory required". Under it, a trace that
looks clean only because the n-gram fallback ran is a gate failure, in
`loop_detect_semantic` and in `acceptance_drift` alike — a passing n-gram score
says two texts share characters, not that they mean the same thing. A real loop
is still reported as a loop rather than as a missing measure, and the config
rejects `require_embeddings = true` with `enabled = false` at load.

**`prove_disjoint` passed trivially on an unpopulated plan.** It now fails a
multi-node plan whose `levels` are empty, and fails when an audited node appears
in no level at all. A single-node plan still passes, because one node cannot
collide with itself.

**The rollback-failure path was untested.** `tests/test_integrate_git.py` now
injects a failing `git reset --hard` at the subprocess boundary and asserts that
`rolled_back` is False, that the summary says `LEFT DIRTY` rather than claiming a
rollback that did not happen, and exactly what survives on the branch when that
happens.
