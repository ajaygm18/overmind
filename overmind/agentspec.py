"""Generate one Omnigent agent spec per node.

The executor used to build a command line: `--harness`, `--policy`,
`--policy-param cost_budget.max_cost_usd=...`, `--prompt`. Those flags were
inferred, and `AGENT_YAML_SPEC.md` documents something else entirely -- a single
YAML file describing the harness, instructions, tools, sandbox and policies.
Building that file instead replaces guessed flags with a documented interface,
and gives the compiled policies from `policy_export` somewhere to live.

Three rules constrain what this emits.

**Only documented fields.** `ALLOWED_TOP_LEVEL` is the field list from the spec.
A test asserts generated specs stay inside it, so upstream drift surfaces in CI
rather than as an opaque validation error mid-run.

**Nothing is written inside the worktree.** Instructions and specs live under
`.overmind/specs/`. A file dropped into the worktree would appear in
`git status` as an undeclared write and fail the node's own `declared_scope`
gate -- the orchestrator framing the worker for its own bookkeeping.

**`sandbox.type` is omitted.** The spec says omitting it selects the platform
default (`linux_bwrap` on Linux, `darwin_seatbelt` on macOS), so one generated
file stays valid on both. Pinning a backend here would break macOS for no gain.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .config import Config
from .models import TaskNode
from .policy_export import compile_policies, role_name

SPEC_ROOT = Path(".overmind/specs")
AGENTS_DIR = Path("agents")

# Top-level keys documented in AGENT_YAML_SPEC.md at
# 09a035ebb3765054f8312c0d5eba8a5e7f818d49. Emitting anything else is a bug.
ALLOWED_TOP_LEVEL = frozenset(
    {
        "name",
        "prompt",
        "instructions",
        "executor",
        "tools",
        "policies",
        "params",
        "os_env",
        "terminals",
        "async",
        "cancellable",
        "timers",
    }
)


class AgentSpecError(Exception):
    pass


def role_persona(role: str, agents_dir: Path = AGENTS_DIR) -> str:
    """The role's voice, read from `agents/<role>.yaml`.

    Those files predate this module and describe how each role should behave.
    Generating specs without them would leave five hand-written role
    definitions as dead weight and silently drop the guidance in them, so their
    prompt text is read and merged into the generated instructions instead.
    """
    path = agents_dir / f"{role}.yaml"
    if not path.exists():
        raise AgentSpecError(f"no agent definition at {path} for role {role!r}")
    try:
        loaded = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise AgentSpecError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(loaded, dict):
        raise AgentSpecError(f"{path} must contain a mapping")
    text = loaded.get("instructions") or loaded.get("prompt") or ""
    return str(text).strip()


def build_instructions(node: TaskNode, persona: str = "") -> str:
    """The full instruction text for one node.

    The acceptance criterion is passed verbatim and the worker is told not to
    paraphrase it, because a restated criterion is a criterion the worker has
    already begun negotiating with.
    """
    exit_line = f"EXIT: this task is done when {node.exit_check.kind}"
    if node.exit_check.command:
        exit_line += f" via `{node.exit_check.command}`"

    sections: list[str] = []
    if persona:
        sections.append(persona)

    sections.append(
        "\n".join(
            [
                f"TASK: {node.intent}",
                "",
                f"ACCEPTANCE (verbatim, do not paraphrase): {node.acceptance}",
                "",
                f"You may modify only: {', '.join(node.writes) or '(nothing)'}",
                f"You may read: {', '.join(node.reads) or '(anything in the tree)'}",
                "",
                exit_line,
                "",
                "You are confined to this worktree. Writes outside the paths above are "
                "denied at the tool call, not reported later -- if you believe you need "
                "another path, say so and stop rather than working around the denial.",
                "",
                "Before finishing, emit a line `DECISION: <what you chose and why>` for "
                "every choice you made that was not dictated by the task above. "
                "Undeclared decisions that surface later in review fail the run.",
            ]
        )
    )

    if node.inspects:
        sections.append(
            f"You are inspecting node {node.inspects}. Compare its output to the "
            "ACCEPTANCE text above, not to your own summary of it. Start your verdict "
            "with PASS or FAIL."
        )

    return "\n\n".join(sections).strip() + "\n"


def build_executor(node: TaskNode, cfg: Config) -> dict[str, Any]:
    """The `executor:` block.

    `model` and `auth` are emitted only when configured. The spec allows CLI
    flags to supply a missing model, and several harnesses (cursor, copilot,
    kimi) authenticate from the ambient environment and take no `auth` block at
    all -- so inventing one would break exactly the harnesses that need it least.
    """
    if not node.harness:
        raise AgentSpecError(f"node {node.id} was not routed to a harness")

    executor: dict[str, Any] = {"harness": node.harness}

    vendor = node.vendor or ""
    model = cfg.models.get(vendor)
    if model:
        executor["model"] = model

    auth = cfg.auth.get(vendor)
    if auth:
        # Emitted verbatim. The spec deprecates the top-level
        # `executor.profile` shorthand, so profiles belong inside auth.
        executor["auth"] = dict(auth)

    return executor


def build_os_env(worktree: Path | str) -> dict[str, Any]:
    """The `os_env:` block: this worktree, and nothing else.

    `sandbox.type` is deliberately absent so the platform default applies.
    `write_paths` is the worktree alone -- never `.`, which would be the whole
    repository and would make every isolation claim in this project false.
    """
    tree = str(worktree)
    return {
        "type": "caller_process",
        "cwd": tree,
        "sandbox": {
            "write_paths": [tree],
            "allow_network": True,
        },
    }


def build_spec(
    node: TaskNode,
    cfg: Config,
    worktree: Path | str,
    instructions_path: Path | str,
) -> dict[str, Any]:
    """Assemble the full spec for one node."""
    spec: dict[str, Any] = {
        "name": f"overmind-{node.id}",
        "instructions": str(instructions_path),
        "executor": build_executor(node, cfg),
        "os_env": build_os_env(worktree),
        "policies": compile_policies(node, cfg, worktree),
        "async": False,
        "cancellable": True,
    }

    if cfg.tools:
        spec["tools"] = {name: dict(decl) for name, decl in cfg.tools.items()}

    unknown = set(spec) - ALLOWED_TOP_LEVEL
    if unknown:
        raise AgentSpecError(f"generated spec has undocumented fields: {sorted(unknown)}")
    return spec


def write_spec(
    node: TaskNode,
    cfg: Config,
    run_id: str,
    worktree: Path | str,
    *,
    root: Path = SPEC_ROOT,
    agents_dir: Path = AGENTS_DIR,
) -> Path:
    """Write the instructions and spec files, and return the spec path.

    Both files live outside the worktree on purpose: anything written inside it
    would be an undeclared write in `git status`, and the node's own
    `declared_scope` gate would fail it for the orchestrator's bookkeeping.
    """
    out = Path(root) / run_id
    out.mkdir(parents=True, exist_ok=True)

    persona = role_persona(role_name(node), agents_dir)
    instructions_path = out / f"{node.id}.md"
    instructions_path.write_text(build_instructions(node, persona))

    spec_path = out / f"{node.id}.yaml"
    spec = build_spec(node, cfg, worktree, instructions_path)
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False, default_flow_style=False))
    return spec_path
