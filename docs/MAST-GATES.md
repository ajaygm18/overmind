# MAST failure modes → enforced gates

Source: *Why Do Multi-Agent LLM Systems Fail?* — Cemri et al., NeurIPS 2025 ([arXiv:2503.13657](https://arxiv.org/abs/2503.13657)). 14 failure modes derived from 200 traces across MetaGPT, ChatDev, HyperAgent, OpenManus, AppWorld, Magentic and AG2, inter-annotator κ=0.88. 79% of failures are specification or coordination problems, not model capability.

Each mode below maps to a gate in `overmind/gates.py`, or is explicitly marked unmapped. Unmapped is an honest state; silently ignored is not.

## Category 1 — Specification issues

| Mode | Gate | Mechanism |
|---|---|---|
| Disobey task specification | `spec_conformance` | Planner emits an `acceptance` field per node; gate diffs the result against it before the node can close |
| Disobey role specification | `role_scope` | Each `agents/*.yaml` declares allowed tools. A node calling outside its declared set is terminated, not warned |
| Step repetition | `loop_detect` | Receipt ledger hashes (node_id, tool, args). Third identical hash halts the node |
| Loss of conversation history | `context_carry` | Serialized chains inherit the predecessor's full transcript. Enforced at the linearity gate, not left to the prompt |
| Unaware of termination conditions | `explicit_exit` | Every node needs a machine-checkable exit. A node whose exit is "the model says done" fails validation at plan time |

## Category 2 — Inter-agent misalignment

| Mode | Gate | Mechanism |
|---|---|---|
| Conversation reset | `checkpoint_continuity` | Resume reads the OMA checkpoint; a reset that loses prior decisions is a hard failure |
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

14 of 14 modes mapped. Honest caveats about the mappings:

- `decision_surface` and `action_trace` are **detective, not preventive** — they catch the failure in the receipt after the fact rather than stopping it. Preventing them requires reading intent, which is not solvable at the orchestration layer.
- `ambiguity_halt` uses a model-scored threshold, so it inherits the scorer's calibration error. It will halt on some clear goals and pass some ambiguous ones.
- `loop_detect` catches identical repetition. Semantically identical work with different arguments passes.

## Why gates instead of better prompts

Every mode above has been "fixed with prompting" somewhere in the ecosystem, and the MAST data was collected from frameworks that had already tried. Prompt-level fixes fail under distribution shift because compliance is probabilistic. A gate that inspects an artifact — a diff, a test exit code, a schema validation — either passes or does not, and does not degrade when the model changes.
