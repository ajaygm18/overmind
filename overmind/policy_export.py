"""Compile Overmind's per-node judgement into an Omnigent policy block.

This is the seam between Overmind's plan model and Omnigent's enforcement
engine. It converts one `TaskNode` into the `policies:` mapping of that node's
generated agent YAML.

One rule governs what may appear here: **only handler paths that upstream
actually documents.** `POLICIES.md` gives exact dotted paths for
`safety.max_tool_calls_per_session`, `safety.ask_on_os_tools` and
`cost.cost_budget`, so those are emitted verbatim. It documents
`block_working_dir_changes` and `risk_score_policy` by behaviour but not by
import path, so those are *not* emitted -- `policies.runtime.dir_guard` covers
the first. Guessing an import path yields a policy that fails at session start,
which is the opposite of a guardrail.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .config import Config

# Upstream handler paths, quoted from docs/POLICIES.md at
# 09a035ebb3765054f8312c0d5eba8a5e7f818d49. Anything not in this map is not
# emitted, however useful it looks in the prose.
UPSTREAM_MAX_TOOL_CALLS = "omnigent.policies.builtins.safety.max_tool_calls_per_session"
UPSTREAM_COST_BUDGET = "omnigent.policies.builtins.cost.cost_budget"
UPSTREAM_ASK_ON_OS = "omnigent.policies.builtins.safety.ask_on_os_tools"

OVERMIND_SCOPE = "overmind.policies.runtime.scope_guard"
OVERMIND_DIR = "overmind.policies.runtime.dir_guard"
OVERMIND_ROLE = "overmind.policies.runtime.role_guard"
OVERMIND_LOOP = "overmind.policies.runtime.loop_guard"

# Roles that inspect. A verifier runs tests and reads code; it does not edit,
# because a verifier that can edit can make its own check pass.
INSPECTION_ROLES = frozenset({"verifier", "reviewer", "researcher", "planner"})

# Fraction of a node's budget at which the user is asked whether to continue.
# One threshold, not several: a node is a single task, and three prompts inside
# one task is a worse experience than one.
ASK_AT = 0.6


@runtime_checkable
class NodeLike(Protocol):
    """The part of `TaskNode` this compiler needs.

    Narrowed to a protocol on purpose. The compiler has no business reading a
    node's exit check or dependencies, and a narrow contract lets the tests
    exercise it without constructing a full plan.
    """

    id: str
    writes: list[str]
    budget_usd: float | None


def role_name(node: object) -> str:
    """The node's role as a lowercase string, whatever the enum stores."""
    role = getattr(node, "role", "")
    return str(getattr(role, "value", role)).strip().lower()


def may_write(node: object) -> bool:
    """Whether this node's role is allowed to produce edits at all."""
    return role_name(node) not in INSPECTION_ROLES


def compile_policies(
    node: NodeLike,
    cfg: Config,
    worktree: Path | str,
    *,
    ask_on_shell: bool | None = None,
) -> dict[str, dict[str, Any]]:
    """Build the `policies:` block for one node's agent YAML.

    Ordering matters: Omnigent evaluates in declaration order and DENY
    short-circuits the rest. The cheap structural checks are declared first so
    an obviously bad call never reaches the similarity computation.
    """
    tree = str(worktree)
    writes = [str(p) for p in (node.writes or [])]
    shell_ask = cfg.policies.ask_on_shell if ask_on_shell is None else ask_on_shell

    policies: dict[str, dict[str, Any]] = {
        "overmind_worktree_confinement": {
            "type": "function",
            "handler": OVERMIND_DIR,
            "factory_params": {"allowed_dirs": [tree]},
        },
        "overmind_role_boundary": {
            "type": "function",
            "handler": OVERMIND_ROLE,
            "factory_params": {
                "role": role_name(node),
                "may_write": may_write(node),
            },
        },
        "overmind_declared_scope": {
            "type": "function",
            "handler": OVERMIND_SCOPE,
            "factory_params": {
                "write_paths": writes,
                "ask_on_shell": bool(shell_ask),
            },
        },
        "overmind_loop_guard": {
            "type": "function",
            "handler": OVERMIND_LOOP,
            "factory_params": {"threshold": 0.92, "window": 3},
        },
        "overmind_tool_call_limit": {
            "type": "function",
            "handler": UPSTREAM_MAX_TOOL_CALLS,
            "factory_params": {"limit": cfg.policies.max_tool_calls_per_session},
        },
    }

    budget = node.budget_usd
    if budget and budget > 0:
        # Overmind allocates across the plan; Omnigent enforces per session.
        # Its hard limit is a downgrade gate rather than a stop, which is
        # better behaviour than anything this repo would write, so it is left
        # to do its job untouched.
        policies["overmind_node_budget"] = {
            "type": "function",
            "handler": UPSTREAM_COST_BUDGET,
            "factory_params": {
                "max_cost_usd": round(float(budget), 4),
                "ask_thresholds_usd": [round(float(budget) * ASK_AT, 4)],
            },
        }

    return policies


def interactive_policies() -> dict[str, dict[str, Any]]:
    """Approval-on-every-OS-tool, for supervised runs only.

    Deliberately not part of `compile_policies`. `ask_on_os_tools` prompts on
    every read, write, edit and shell call, which is right when a human is
    watching and fatal to an unattended run -- and a guardrail that trains
    people to click approve is worse than none. Opt in explicitly.
    """
    return {
        "overmind_supervised": {
            "type": "function",
            "handler": UPSTREAM_ASK_ON_OS,
        }
    }
