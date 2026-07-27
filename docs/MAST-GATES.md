# MAST failure modes → enforced gates

Source: *Why Do Multi-Agent LLM Systems Fail?* — Cemri et al., NeurIPS 2025 ([arXiv:2503.13657](https://arxiv.org/abs/2503.13657)). 14 failure modes derived from 200 traces across MetaGPT, ChatDev, HyperAgent, OpenManus, AppWorld, Magentic and AG2, inter-annotator κ=0.88. 79% of failures are specification or coordination problems, not model capability.

Each mode below maps to a gate in `overmind/gates.py`, or is explicitly marked unmapped. Unmapped is an honest state; silently ignored is not.

## Category 1 — Specification issues

| Mode | Gate | Mechanism |
|---|---|---|
| Disobey task specification | `spec_conformance`, `declared_scope` | Planner emits an `acceptance` field per node; gate diffs the result against it before the node can close. `declared_scope` additionally reads the node's real writes out of `git status`/`git diff` and fails any path it never declared |
| Disobey role specification | `role_scope` | Each `agents/*.yaml` declares allowed tools. A node calling outside its declared set is terminated, not warned |
| Step repetition | `loop_detect_semantic` | Cosine similarity over consecutive tool calls, using Ruflo `embeddings_generate` with an offline character-n-gram fallback. Three consecutive calls above threshold halt the node. Replaces the original hash-equality `loop_detect`, which it subsumes (identical calls score 1.0) |
| Loss of conversation history | `context_carry` | Serialized chains inherit the predecessor's full transcript. Enforced at the linearity gate, not left to the prompt |
| Unaware of termination conditions | `explicit_exit` | Every node needs a machine-checkable exit. A node whose exit is "the model says done" fails validation at plan time |

## Category 2 — Inter-agent misalignment

| Mode | Gate | Mechanism |
|---|---|---|
| Conversation reset | `checkpoint_continuity` | `overmind resume` reloads the plan snapshot, carries every decision found in the receipts back into memory, and re-reads them. A resume that cannot read its prior decisions back is refused rather than run |
| Fail to ask for clarification | `ambiguity_halt` | Planner scores goal ambiguity. Above threshold, the run halts for human input before spending anything |
| Task derailment | `acceptance_drift` | Verify node compares output against the original `acceptance`, never against the worker's own restatement of it |
| Information withholding | `decision_surface` | Nodes must emit decisions to the receipt ledger. Undeclared decisions surfacing later in a diff fail review |
| Ignored other agent's input | `review_ack` | Review comments must be either applied or explicitly rejected with a reason. Silence fails the gate |
| Reasoning-action mismatch | `action_trace` | Tool calls are diffed against the stated plan for the node. Divergence flags for review |

## Category 3 — Task verification

| Mode | Gate | Mechanism |
|---|---|---|
| Premature termination | `exit_proof` | Node closes only on the machine-checkable exit condition, never on a model assertion |
| No or incomplete verification | `verify_required` | Structural: the plan rewrite inserts a verify node after every write node. Cannot be disabled by config |
| Incorrect verification | `cross_vendor_verify` | Verifier runs on a different vendor than the author. Same-vendor pairing is a hard error |

## Coverage

14 of 14 modes mapped, plus the two beyond-MAST failures below.

That is enforced, not asserted. [MAST-COVERAGE.md](MAST-COVERAGE.md) is generated
from the traces in [`../tests/mast/`](../tests/mast/), and
`tests/test_mast_coverage.py` runs every trace through the real gate. A mode in
the tables above with no trace fails the suite, as does a mode with traces in
only one direction — a gate that rejects everything covers nothing.

Honest caveats about the mappings:

- `decision_surface` and `action_trace` are **detective, not preventive** — they catch the failure in the receipt after the fact rather than stopping it. Preventing them requires reading intent, which is not solvable at the orchestration layer.
- `ambiguity_halt` uses a model-scored threshold, so it inherits the scorer's calibration error. It will halt on some clear goals and pass some ambiguous ones.
- `loop_detect_semantic` degrades when Ruflo is unavailable: the n-gram fallback catches rephrasings that stay lexically close (`device_code` → `device code`) but will miss a genuine restatement in different words. The gate reports which measure it used, so a `via ngram` result should be read as the weaker check.
- `action_trace` reads the harness's reported tool calls and can therefore be defeated by incomplete reporting. `declared_scope` exists because of that: it measures the same question from git, which the agent does not get to narrate.

## Beyond MAST

Two gates address a failure MAST does not enumerate, described in Cognition's *Don't Build Multi-Agents*: two agents working concurrently make **conflicting implicit decisions** against the same file, and the result can merge cleanly while being incoherent.

| Failure | Gate | Mechanism |
|---|---|---|
| Conflicting implicit decisions (textual) | `clean_merge` | Worktrees are merged back sequentially in plan order, never as an octopus merge. A conflict aborts and resets the base branch to a recorded SHA, because a half-integrated tree is worse than a failed run |
| Conflicting implicit decisions (semantic) | `prove_disjoint` | The rewriter authorises concurrency from *declared* file sets. After execution, the same disjointness question is re-asked of the *measured* sets. An overlap means the proof rested on bad input, and the finding is written to memory so the next plan for this repo declares the path |

`prove_disjoint` is the important one. `clean_merge` only catches collisions git can see; two agents editing different lines of the same file to opposite ends merge without complaint, and only the measured-overlap check notices.

## Why gates instead of better prompts

Every mode above has been "fixed with prompting" somewhere in the ecosystem, and the MAST data was collected from frameworks that had already tried. Prompt-level fixes fail under distribution shift because compliance is probabilistic. A gate that inspects an artifact — a diff, a test exit code, a schema validation — either passes or does not, and does not degrade when the model changes.
