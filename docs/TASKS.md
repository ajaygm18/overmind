# Overmind — delivery plan

Spec-driven. Each task states a **spec** (what must become true and why),
**subtasks** (the ordered work), **acceptance** (falsifiable criteria), and
**files**. A task is closable only when every acceptance criterion is observably
true — not when the code exists.

Status keys: `todo` · `doing` · `done` · `blocked`

| # | Task | Depends on | Status |
| --- | --- | --- | --- |
| T01 | Policy compiler: gates → Omnigent policies | — | `done` |
| T02 | Agent YAML generation conforming to upstream spec | T01 | `done` |
| T03 | Worktree confinement (conflict C1) | T02 | `done` |
| T04 | Wire `restatement_fidelity` into `acceptance_drift` | — | `done` |
| T05 | Git fixture harness; test `integrate.py` for real | — | `done` |
| T06 | Strengthen the three weakest gates | — | `done` |
| T07 | Upstream contract tests that actually assert | T02 | `done` |
| T08 | Bridge robustness and plan schema validation | — | `done` |
| T09 | Failure-mode evaluation harness | T01, T05 | `done` |
| T10 | Receipt → OpenTelemetry export | — | `done` |
| T11 | Reproducible dev environment | T02 | `done` |
| T12 | Release readiness: pins, packaging, quickstart | all | `done` |

### Deviations from spec

Recorded here because a task marked `done` that quietly did something else is
worse than one marked `blocked`.

- **T02 / T03, spec location.** The spec said write `instructions` to a file *in
  the worktree*. Doing so would make the orchestrator's own bookkeeping an
  undeclared write inside the node's scope, failing that node's `declared_scope`
  gate on its first tool call. Specs and instructions live in
  `.overmind/specs/<run_id>/` instead, referenced by absolute path.
- **T03, policy name.** `block_working_dir_changes` is named in `POLICIES.md`
  without a dotted handler path, and only three builtin handler strings are
  verbatim-confirmed. Emitting a guessed import path would fail at session
  start, so confinement is enforced by an owned `dir_guard` handler plus
  `sandbox.write_paths`. The acceptance criteria are met by different means; the
  criterion "every generated spec contains `block_working_dir_changes`" is
  **not** met and should not be claimed.
- **T04, fallback.** The spec kept word overlap as the fallback when no
  embedding source is available. There is no such state:
  `restatement_fidelity_measured` degrades to n-grams internally and reports
  which measure it used. Overlap is reported as a second opinion and is never
  load-bearing, which satisfies the final subtask (remove the dead overlap-only
  branch) by there being no branch.
- **T06, `spec_conformance`.** The spec asked it to check exit-kind satisfaction
  and declared-vs-actual writes. `exit_proof` and `action_trace` already gate
  exactly those, and one failure reported by three gates reads as three
  problems. It now refuses unparseable and evidence-free verdicts instead.
  `role_scope` did take the measured-diff check the spec asked for.
- **T07, what it found.** The job was worse than unasserted. It grepped
  `omnigent run --help` for `--harness`, `--cwd` and `--policy` — flags T02
  removed from `executor.py` — so it was guarding a contract this repo no longer
  has, and `continue-on-error: true` meant nobody would have noticed either way.
  The job is now blocking, and the two failure kinds are separated in the tests
  rather than by a job-level flag: unreachable upstreams `skip`, changed
  upstreams fail. `test_omnigent` derives its module paths from
  `policy_export`'s own handler constants, so it also catches a typo in a string
  we emit.
- **T08, what it found.** Three silent repairs, not one: an unrecognised
  `exit_check.kind` became `tests_pass`, a `command_exit_zero` with no command
  became `tests_pass`, and a missing `role` became `implementer` — which can hand
  write access to a task meant as research. The client was equally permissive,
  accepting any 200 carrying a `nodes` key of any shape and coercing a
  non-numeric `ambiguity` to `0.0`, which would silently disable
  `ambiguity_halt`. Validation now lives in `bridge/schema.ts`, rejections name
  the field by path, and `PlanInvalid` subclasses `PlanRejected` so existing
  handlers keep working.
- **T08, dry-run scope.** `overmind plan --dry-run` prints the POST body and the
  recalled decisions, then stops. It does not validate a plan offline, because
  there is no plan until the coordinator answers. Offline validation of the
  *reaction* to each possible answer is in `tests/test_bridge_contract.py`.
- **T09, where the generated table lives.** The spec said regenerate the coverage
  table *in* `docs/MAST-GATES.md`. A generator that rewrites one section of a
  hand-written document either clobbers the prose around it or depends on marker
  comments that rot. The generated table is `docs/MAST-COVERAGE.md`, written
  whole by `tools/gen_coverage.py`; `MAST-GATES.md` keeps its hand-written
  mapping and links to it. `--check` compares parsed rows rather than bytes, so
  the prose in the generated file is still editable.
- **T09, `prove_disjoint` levels.** That gate compares nodes within a
  `plan.levels` entry, and `Plan.model_validate` does not compute levels — the
  rewriter does, and it is not in the loop for a fixture. The driver reads a
  trace as one concurrent level unless the trace says otherwise, which is what
  both of its traces mean. A trace can override it with a `levels` key.
- **T09, `mast_mode` asserted by prefix.** `clean_merge` and `prove_disjoint`
  both report `conflicting implicit decisions`, while the docs distinguish
  `(textual)` from `(semantic)`. The assertion is a prefix match rather than
  equality, and it runs only on blocking results: several gates leave
  `mast_mode` empty on PASS, which is right, since there is no failure to
  classify.
- **T10, no OpenTelemetry SDK.** The spec assumed the SDK. OTLP/JSON is a
  documented wire shape and `httpx` is already a dependency, so `otel.py` builds
  the payload directly. Pulling in a tracer provider, span processor, exporter,
  and context propagation to POST data that is already complete and at rest
  would be a large dependency for no capability.
- **T10, durations are derived and say so.** `Receipt` has `at` — when the entry
  was appended — and no start timestamp, so a node's span runs from the previous
  ledger entry to its own. That approximates wall time; it does not measure it.
  Every span carries `overmind.timing.source="derived-from-ledger-order"`, the
  CLI prints the same caveat, and a test asserts the attribute is present.
  **The real fix is a `started_at` field on `Receipt`; until then this is a
  limitation and belongs in `docs/LIMITATIONS.md` (T12), not a solved item.**
- **T10, tool arguments are never exported.** Ledger redaction is opt-in
  (`ReceiptConfig.redact_tool_args`), so a receipt on disk may hold a secret an
  operator chose not to scrub locally. Spans go to third-party backends, so only
  tool names and counts leave the machine. A test greps a rendered payload for a
  fixture secret.
- **T11, two parts stay on the host.** The spec said containerise the dev
  environment. Only the bridge went in. Ruflo memory is a stdio MCP subprocess
  owned by the Python process — a container would put a network boundary across
  a pipe that has no network transport — and Omnigent needs the real worktree and
  the operator's provider credentials, so containerising it means bind-mounting
  the repository and forwarding credentials inward. Both reasons are in
  `docs/DEVELOPMENT.md` next to the table of what runs where.
- **T11, Jaeger instead of a collector.** T10 emits OTLP/HTTP and Jaeger ingests
  it directly on 4318 with one environment variable. An OTLP collector in front
  would add a config file to maintain for no capability at this scale.
- **T11, the image is repeatable, not reproducible.** `bridge/` has no
  `package-lock.json`, so `npm install` in the image resolves within the declared
  semver ranges at build time and two builds a month apart can differ. Generating
  a lockfile requires an npm run, so this is recorded rather than claimed:
  **`docs/LIMITATIONS.md` (T12) must list it.**
- **T12, what the README audit found.** Five claims were stale or contradicted by
  the repo's own documents. The file tree listed 7 modules and omitted 8 — every
  module written after the first cut. "Roughly 900 lines of Python" had stopped
  being true, and a number that needs updating every commit is not worth
  asserting, so it is gone rather than corrected. "Maps all 14 modes" was a claim
  about a document; it now points at the generated coverage table. "Adapters are
  pinned and contract-tested" was half true — `PINS.md` states plainly that
  nothing is pinned and why, so the README could not keep saying otherwise.
  "`gates.py` catches this after the fact via receipts, not before" was
  superseded by the ADR-003 amendment and T01's in-session policies.
- **T12, `PINS.md` corrects ADR-001 rather than restating it.** ADR-001 says
  adapters are "pinned and contract-tested". The contract tests exist and are
  blocking; the pinning does not. Every Python dependency is a lower bound, there
  is no `uv.lock` and no `package-lock.json`, and Omnigent and Ruflo install as
  latest on purpose. `PINS.md` says so in the opening paragraph instead of
  presenting the ADR's wording as current fact.
- **T12, no version bump and no publish.** The spec listed "bump version, publish
  to PyPI". `0.1.0` stays, because nothing has been released to bump from and a
  version number should mark a release rather than a task. Publishing needs a
  PyPI account and a claimed name, which is not a code change and is not done
  here; `overmind version` reports `0.1.0 (from source, not installed)` when run
  from a checkout, which is the honest answer.
- **Post-T12 remediation.** Writing a limitation down is not fixing it. Every
  entry in `LIMITATIONS.md` that named a fix was attempted, in this order:
  measured span timing (`Receipt.started_at`, set in `executor.execute` before
  the worktree exists, exported as `measured-from-receipt` only when every node
  in the ledger has it); `prove_disjoint` refusing an unpopulated `levels` and
  an audited node that appears in no level; `[memory] require_embeddings`,
  which turns the silent n-gram fallback into a gate failure in
  `loop_detect_semantic` and `acceptance_drift` both; the rollback-failure
  fixture; dependency bounds and exact bridge pins; and the ambiguity
  calibration corpus. Each landed with tests. `LIMITATIONS.md` was then
  rewritten to list only what is still open, with the closed items kept in a
  final section so a reader of the earlier version can see where they went.
- **Ambiguity calibration is half a fix, and says so.** `overmind/calibration.py`
  and `tests/mast/ambiguity.jsonl` supply the corpus, the format, and a chooser
  that maximises Youden's J; `tools/calibrate_ambiguity.py` exits non-zero
  rather than printing a threshold when no scores exist. The scores cannot be
  produced here — they are the planner's self-report and need a live
  coordinator — so 0.65 still ships, labelled provisional in three places, and
  a test asserts the corpus is still unscored so that the day it changes, CI
  says so. Fabricating scores would have produced a calibrated-looking number
  nobody would re-examine.
- **Lockfiles remain absent; bounds do not.** Python requirements are now
  bounded at the next major and `bridge/package.json` pins exact versions, but
  `uv.lock` and `package-lock.json` need a resolver run this repo's tooling
  cannot perform. Recorded as still open rather than claimed.
- **`block_working_dir_changes` was left unmet deliberately.** The dotted
  handler path is still not verbatim-confirmed from upstream, and a guessed
  import fails at session start rather than at review. The owned `dir_guard`
  plus `sandbox.write_paths` stays (ADR-010).
- **T05, `LEFT DIRTY` path.** Subtask 7 (simulate a rollback failure) is not
  covered. Forcing `git reset --hard` to fail requires making the object store
  unwritable mid-merge, which is platform-specific enough to be its own
  liability. The branch is reachable and reported; it is untested, and
  `docs/LIMITATIONS.md` will say so in T12.

---

## T01 — Policy compiler: gates → Omnigent policies

**Spec.** Gates whose decision time is *in-session* (`loop_detect_semantic`,
`declared_scope`, `role_scope`) currently run post-hoc, after the budget is spent
and the bad write is on disk. Omnigent's policy engine is the correct enforcement
point: it evaluates every tool call, returns ALLOW / DENY / ASK, sees accumulated
`session_state` and `usage`, and short-circuits on DENY. Compile Overmind's gate
configuration into Omnigent policy declarations so the failure is *prevented*
rather than *reported*. Retain the receipt gates as defence in depth — a harness
that under-reports tool calls defeats policies but not `git status`.

**Subtasks.**
1. `overmind/policy_export.py` with `compile_policies(node, cfg) -> dict` emitting
   the `policies:` block for a node's agent YAML.
2. `overmind/policies/runtime.py` — the handlers Omnigent imports, matching the
   documented signature `(event: PolicyEvent) -> PolicyResponse | None`, factory
   form with `factory_params`.
3. `scope_guard(write_paths)` — DENY `sys_os_write` / `sys_os_edit` outside the
   node's declared paths; ASK on ambiguous shell redirection rather than guessing.
4. `loop_guard(threshold, window)` — accumulate a rolling description of recent
   tool calls in `session_state` via `state_updates: append`; DENY on a semantic
   loop, reusing `semantic.find_loop` so one algorithm serves both paths.
5. Always include upstream builtins: `max_tool_calls_per_session` and
   `cost_budget` with the node's allocated share.
6. Export `POLICY_REGISTRY` so the handlers are discoverable in Omnigent's UI.
7. Unit tests driving the documented event shape directly — no Omnigent process.

**Acceptance.**
- `compile_policies` output validates against every field documented in
  `POLICIES.md` (`type`, `handler`, `factory_params`).
- `scope_guard` DENYs a write outside scope and ALLOWs one inside, proven by test.
- `loop_guard` DENYs on the third semantically-equivalent call and ALLOWs genuine
  progress; the identical case still trips.
- Every handler returns `None` for phases it does not judge (abstains correctly).
- `semantic.find_loop` has exactly one implementation, used by both paths.

**Files.** `overmind/policy_export.py`, `overmind/policies/__init__.py`,
`overmind/policies/runtime.py`, `tests/test_policy_export.py`

---

## T02 — Agent YAML generation conforming to upstream spec

**Spec.** `executor.py` currently shells out with `_policy_flags`, which is
guesswork against a CLI surface. `AGENT_YAML_SPEC.md` documents the real
contract: one YAML file per agent with `executor`, `os_env`, `tools`, `policies`.
Generating a spec-conformant YAML per node replaces flag guessing with a
documented interface, and is the only way T01's policies can be attached.

**Subtasks.**
1. `overmind/agentspec.py` — `build_spec(node, run_id, worktree, cfg) -> dict`.
2. Map `Role` → `executor.harness` + `model` via the router; emit `executor.auth`
   in documented form (`type: databricks` + profile, or `api_key`), never the
   legacy top-level `executor.profile` the spec deprecates.
3. `os_env`: `type: caller_process`, `cwd` = worktree, `sandbox.write_paths` =
   worktree only. Omit `sandbox.type` so the platform default applies, keeping one
   YAML valid on Linux and macOS.
4. `instructions` written to a file in the worktree, referenced by path — the spec
   recommends this over inline `prompt` for long instructions.
5. Attach `policies` from T01 and `tools` from config.
6. Write to `.overmind/specs/<run_id>/<node_id>.yaml`; `executor.execute` invokes
   `omnigent run <spec>` instead of assembling flags.
7. Round-trip test: generated YAML parses and contains every required key.

**Acceptance.**
- No `_policy_flags` string assembly remains in `executor.py`.
- Generated YAML contains only keys documented in `AGENT_YAML_SPEC.md`; a test
  asserts against an explicit allowlist of field names so upstream drift is caught.
- `sandbox.write_paths` is exactly the node worktree — never `.`, never `/`.
- Reviewer nodes emit a different `executor.harness` than the author they review,
  asserted at the YAML level and not only in the router.
- Specs are gitignored and reproducible from plan + config.

**Files.** `overmind/agentspec.py`, `overmind/executor.py`, `.gitignore`,
`tests/test_agentspec.py`

---

## T03 — Worktree confinement (conflict C1)

**Spec.** Omnigent's `block_working_dir_changes` blocks `git worktree add` and
`cd` by default, parsing chained and wrapped commands to prevent bypasses. This
conflicts head-on with an executor built on worktrees. The resolution hardens the
design: Overmind creates the worktree **outside** the sandbox before the session
starts, then *enables* the policy with `allowed_dirs` pinned to that worktree. The
agent is confined to a directory it did not create and cannot leave — which
answers a question the original design had no answer to.

**Subtasks.**
1. Emit `block_working_dir_changes` in every generated spec with
   `block_cd: true`, `block_worktree: true`, `allowed_dirs: [<worktree>]`.
2. Assert in `preflight()` that worktree creation happens before spec generation,
   with an explicit ordering test.
3. Confirm `executor.make_worktree` runs in the host process, never via a
   sandboxed tool.
4. Document the interaction in `docs/upstream/omnigent.md` §C1 and in `SECURITY`
   notes.
5. Regression test: no generated spec ever omits this policy.

**Acceptance.**
- Every generated spec contains `block_working_dir_changes` with a non-empty
  `allowed_dirs`.
- A test proves `allowed_dirs` equals the node's worktree and nothing broader.
- No code path issues `git worktree` from inside a sandboxed session.

**Files.** `overmind/agentspec.py`, `overmind/executor.py`,
`tests/test_confinement.py`

---

## T04 — Wire `restatement_fidelity` into `acceptance_drift`

**Spec.** `semantic.restatement_fidelity` exists, is tested, and is used by
nothing. `gates.acceptance_drift` still uses a 0.4 word-overlap heuristic that
fails on correct paraphrase and passes on shared vocabulary. Computing a better
measure and not using it is the same defect class as advertising a command that
does not exist.

**Subtasks.**
1. Change `acceptance_drift` to call `restatement_fidelity`, keeping word overlap
   as the fallback when no embedding source is available.
2. Report which measure was used in `GateResult.detail`, mirroring
   `Similarity.source`, so a weak measurement is never read as a strong one.
3. Calibrate the threshold against fixtures: faithful paraphrase must pass,
   silently narrowed acceptance must fail.
4. Remove the now-dead overlap-only branch if the fallback subsumes it.

**Acceptance.**
- A faithfully reworded acceptance criterion passes where the old gate failed it.
- An acceptance criterion narrowed to drop a requirement fails.
- `detail` names the measure (`ngram` vs `ruflo-embeddings`) in both cases.
- No gate in `gates.py` computes word overlap independently any more.

**Files.** `overmind/gates.py`, `tests/test_gates_and_routing.py`

---

## T05 — Git fixture harness; test `integrate.py` for real

**Spec.** `integrate.py` is the most dangerous module in the repository — it runs
`merge`, `abort`, and `reset --hard` — and its git-touching paths are untested.
Every current test avoids git. A `reset --hard` bug destroys a user's work.
Untested destructive code is the least defensible thing here.

**Subtasks.**
1. `tests/gitfixture.py` — pytest fixture building a real temporary repo:
   initial commit, N worktrees on `overmind/<run>/<node>` branches, deterministic
   author identity, guaranteed cleanup.
2. Test the clean case: two non-overlapping branches merge; base contains both.
3. Test the conflict case: two branches edit the same line; assert the merge
   aborts, `head_sha` equals `base_sha`, and the report names the conflicted path.
4. Test dirty-tree refusal, and `allow_dirty` behaviour.
5. Test `commit_worktree` on leftover uncommitted changes.
6. Test that failed and read-only nodes are never merged.
7. Test the `LEFT DIRTY` path by simulating a rollback failure.

**Acceptance.**
- `integrate`, `rollback`, `commit_worktree`, `_conflicted_paths` and
  `_merge_order` are each exercised against a real repository.
- The conflict test asserts the working tree is byte-identical to `base_sha`.
- Fixtures leave no directories behind (verified by an `atexit`-style assertion).
- Tests run offline with no network and no upstream processes.

**Files.** `tests/gitfixture.py`, `tests/test_integrate_git.py`

---

## T06 — Strengthen the three weakest gates

**Spec.** `spec_conformance`, `role_scope`, and `context_carry` are the weakest
gates: they lean on string presence and are close to decorative. A gate that
cannot fail is worse than no gate, because it makes the run look inspected.

**Subtasks.**
1. `spec_conformance`: check the node's acceptance criterion against measured
   evidence — exit kind satisfied, diff non-empty when writes were declared,
   declared writes actually touched — rather than text matching.
2. `role_scope`: derive violations from measured behaviour. A `RESEARCHER` or
   `REVIEWER` that produced a non-empty diff has left its role; a `VERIFIER` whose
   exit check is `MODEL_ASSERTION` has too. Use `introspect` rather than prose.
3. `context_carry`: assert prior decisions were actually readable in the session,
   using the same mechanism `resume` uses to verify carry-over, instead of
   checking that a string appeared.
4. For each, add a test that *fails* on a seeded violation — proving the gate can
   fail at all.

**Acceptance.**
- Each of the three gates has at least one test where a seeded violation makes it
  fail, and one where clean input passes.
- None of the three depends on substring matching against model output.
- `role_scope` catches a reviewer that wrote code, proven by measured diff.

**Files.** `overmind/gates.py`, `overmind/introspect.py`,
`tests/test_gates_strength.py`

---

## T07 — Upstream contract tests that actually assert

**Spec.** The `upstream-contracts` CI job is `continue-on-error: true` and does
not meaningfully assert. Ruflo shipped 1,488 releases in ten months; Omnigent is
alpha with a moving harness matrix. Pinning without verification means breakage
surfaces during a real run instead of in CI.

**Subtasks.**
1. Ruflo: assert the four allowlisted tools appear in the MCP tool list and that
   `memory_store` → `memory_search` round-trips a value.
2. Ruflo: assert `RufloToolNotAllowed` still fires for a name outside the
   allowlist — the allowlist is a security property, not a convenience.
3. Omnigent: assert `omnigent --version` runs and that a generated spec is
   accepted by its validator without executing a model turn.
4. OMA: assert the bridge starts, `GET /health` returns ok, and `POST /plan`
   returns a schema-valid plan for a fixed trivial goal.
5. Keep `continue-on-error` for network flakiness but fail the job on *contract*
   violations, distinguishing "upstream unreachable" from "upstream changed".
6. Emit a summary naming each pinned version actually observed.

**Acceptance.**
- Each of the three upstreams has at least one assertion that fails if its
  interface changes.
- An unreachable upstream is reported differently from a changed one.
- The job prints observed versions, so a silent upstream bump is visible in logs.

**Files.** `tests/contracts/test_ruflo.py`, `tests/contracts/test_omnigent.py`,
`tests/contracts/test_oma.py`, `.github/workflows/ci.yml`

---

## T08 — Bridge robustness and plan schema validation

**Spec.** `bridge/planner.ts` asks an LLM coordinator for a plan and normalises
prose into tasks. It is the widest opening for malformed input in the system, and
it currently trusts its own parsing. A malformed plan that survives normalisation
becomes a spend decision.

**Subtasks.**
1. Define an explicit JSON schema for the `POST /plan` response and validate
   before returning; return HTTP 422 with the offending field on failure.
2. Bound the coordinator: retry once on schema failure with the validation error
   fed back, then fail loudly. No third attempt, no silent repair.
3. Reject at the boundary: unknown `exit_check` kinds, empty `acceptance`,
   self-dependencies, unknown `depends_on` ids.
4. Python side: `_build_plan` surfaces bridge validation errors verbatim rather
   than collapsing them into a generic failure.
5. Add a `--dry-run` planning mode that prints the normalised plan and exits.
6. Tests with fixture payloads: valid, missing field, unknown exit kind, cyclic.

**Acceptance.**
- A plan missing a required field never reaches `linearity.validate`.
- An unknown `exit_check` is rejected at the bridge with a named field.
- Exactly one retry occurs, proven by test.
- `overmind plan --dry-run` prints a plan and spends nothing.

**Files.** `bridge/planner.ts`, `overmind/cli.py`, `bridge/schema.ts`,
`tests/test_bridge_contract.py`

---

## T09 — Failure-mode evaluation harness

**Spec.** Overmind claims its gates catch MAST failure modes. That claim is
currently untested — exactly the defect this repository criticises Ruflo for. A
harness that seeds specific coordination failures into replayable receipt fixtures
and asserts the naming gate catches each one converts the claim into a
measurement.

**Subtasks.**
1. `tests/mast/` fixtures: one synthetic receipt set per targeted failure mode —
   step repetition (rephrased), disobeyed spec (undeclared write), missing
   verification, premature termination, context loss on resume.
2. `tests/test_mast_coverage.py` asserting each fixture is caught by the gate that
   names it, and that a clean fixture trips nothing.
3. A coverage report mapping the 14 modes to `covered` / `partial` /
   `not covered`, generated from the tests rather than hand-maintained.
4. Regenerate `docs/MAST-GATES.md` coverage table from that report so docs cannot
   drift from tests again.
5. Wire into CI as a hard gate.

**Acceptance.**
- Every gate claiming a MAST mode has a fixture that it catches.
- A clean fixture produces zero blocking gate results.
- The coverage table in `MAST-GATES.md` is generated, not written by hand.
- Removing a gate makes a named test fail.

**Files.** `tests/mast/*.jsonl`, `tests/test_mast_coverage.py`,
`tools/gen_coverage.py`, `docs/MAST-GATES.md`

---

## T10 — Receipt → OpenTelemetry export

**Spec.** Receipts are complete but only readable through `overmind replay`. OMA
ships `@open-multi-agent/otel` with span conventions (`oma.task.role`,
`oma.task.meta.<key>`). Emitting spans from receipts makes runs viewable in
standard tooling without adding a second source of truth.

**Subtasks.**
1. `overmind/otel.py` — `export(run_id)` mapping receipts to spans: run span, node
   spans as children, gate results as span events.
2. Follow OMA's attribute naming where it applies; namespace Overmind-specific
   attributes under `overmind.`.
3. `overmind export <run-id> --otlp <endpoint>`, no-op with a clear message when
   unconfigured.
4. Keep it strictly derived — export must be reconstructible from the ledger
   alone, never a parallel write path.
5. Test against an in-memory span exporter.

**Acceptance.**
- Spans reconstruct the run's node tree with correct parent/child nesting.
- Gate failures appear as span events with the gate name and MAST mode.
- Export is pure: running it twice produces identical spans.
- No code path writes telemetry without writing a receipt first.

**Files.** `overmind/otel.py`, `overmind/cli.py`, `tests/test_otel.py`

---

## T11 — Reproducible dev environment

**Spec.** Overmind needs Python 3.12+, Node, `omnigent`, `git`, an npx-reachable
Ruflo, and two vendor credentials. "Works on my machine" is not acceptable for a
tool whose value proposition is verifiability.

**Subtasks.**
1. `docker-compose.yml`: bridge service, an Overmind CLI service sharing the
   workspace volume, health checks.
2. `Dockerfile` for the bridge, pinned Node, `npm ci`.
3. Credentials via env file only — never baked into an image; document the
   two-vendor requirement explicitly.
4. `make dev-up` / `make dev-down`; extend `make doctor` to check every
   prerequisite and print exactly what is missing.
5. Document sandbox availability: `linux_bwrap` needs a Linux host; state what
   degrades on macOS and in containers.

**Acceptance.**
- `make dev-up` yields a healthy bridge answering `GET /health`.
- `overmind doctor` names every missing prerequisite individually, not just
  "setup incomplete".
- No secret appears in any committed file or image layer.
- Quickstart is verified from a clean clone.

**Files.** `docker-compose.yml`, `bridge/Dockerfile`, `Makefile`, `.env.example`,
`docs/DEVELOPMENT.md`

---

## T12 — Release readiness

**Spec.** Pin every upstream, make the version surface honest, and ensure the
README's claims match observable behaviour. Overstating readiness would repeat
the exact failure this project was built in reaction to.

**Subtasks.**
1. Pin exact upstream versions in `pyproject.toml`, `bridge/package.json`, and
   `overmind.toml`'s Ruflo command; record each in a `docs/PINS.md` with the date
   and reason.
2. `overmind version` printing Overmind's version plus each observed upstream
   version.
3. README audit: every claim either demonstrated by a test or labelled as a
   limitation. Remove anything not backed.
4. `docs/LIMITATIONS.md`: what is untested, what needs two credentials, what
   degrades without memory, and the residual first-run mis-declaration risk.
5. ADR-009 (gate placement: in-session vs post-hoc) and ADR-010 (worktree
   confinement via upstream policy).
6. Final consistency pass: no document may describe a fixed weakness as open, or
   an open weakness as fixed.

**Acceptance.**
- Every upstream is pinned to an exact version with a recorded reason.
- `overmind version` reports all four versions.
- No README claim lacks either a test or an explicit caveat.
- ADR set covers every load-bearing decision including the two new ones.
- Docs and code agree in both directions.
