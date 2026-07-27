"""Dispatch to Omnigent. One node, one worktree, one sandboxed session.

Everything about isolation is Omnigent's: bwrap on Linux, seatbelt on macOS,
plus its three-level policy stack. Overmind never shells out to a model
directly, so there is exactly one execution path and it is sandboxed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .models import ExitKind, Receipt, TaskNode

WORKTREE_ROOT = Path(".worktrees")


class ExecutorError(Exception):
    pass


@dataclass
class NodeOutcome:
    node_id: str
    exit_code: int | None
    diff_stat: str | None
    worktree: Path | None
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    tool_calls: list[dict[str, object]] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    stdout_tail: str = ""
    error: str | None = None


def preflight() -> list[str]:
    """Report missing external tools rather than failing halfway into a run."""
    missing = [tool for tool in ("omnigent", "git") if shutil.which(tool) is None]
    return missing


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 3600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - argv list, no shell
        cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
    )


def make_worktree(node: TaskNode, run_id: str) -> Path:
    """Isolate the node. Two sessions never share a tree."""
    WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)
    path = WORKTREE_ROOT / f"{run_id}-{node.id}"
    branch = f"overmind/{run_id}/{node.id}"
    res = _run(["git", "worktree", "add", "-b", branch, str(path)])
    if res.returncode != 0:
        raise ExecutorError(f"git worktree add failed for {node.id}: {res.stderr.strip()[:300]}")
    return path


def _policy_flags(cfg: Config, node: TaskNode) -> list[str]:
    """Overmind ships defaults; Omnigent evaluates them."""
    flags: list[str] = []
    if cfg.policies.ask_on_shell:
        flags += ["--policy", "omnigent.policies.builtins.safety.ask_on_os_tools"]
    flags += [
        "--policy-param",
        f"max_tool_calls_per_session.limit={cfg.policies.max_tool_calls_per_session}",
    ]
    if node.budget_usd:
        flags += ["--policy-param", f"cost_budget.max_cost_usd={node.budget_usd}"]
    return flags


def _agent_file(node: TaskNode) -> Path:
    path = Path("agents") / f"{node.role}.yaml"
    if not path.exists():
        raise ExecutorError(f"no agent definition at {path} for role {node.role}")
    return path


def _prompt(node: TaskNode) -> str:
    """The acceptance criterion is passed verbatim. The worker never restates it."""
    lines = [
        f"TASK: {node.intent}",
        "",
        f"ACCEPTANCE (verbatim, do not paraphrase): {node.acceptance}",
        "",
        f"You may modify only: {', '.join(node.writes) or '(nothing)'}",
        f"You may read: {', '.join(node.reads) or '(anything in the tree)'}",
        "",
        f"EXIT: this task is done when {node.exit_check.kind}"
        + (f" via `{node.exit_check.command}`" if node.exit_check.command else ""),
        "",
        "Before finishing, emit a line `DECISION: <what you chose and why>` for every choice "
        "you made that was not dictated by the task above. Undeclared decisions that surface "
        "later in review fail the run.",
    ]
    if node.inspects:
        lines.append(
            f"\nYou are inspecting node {node.inspects}. Compare its output to the ACCEPTANCE "
            "text above, not to your own summary of it. Start your verdict with PASS or FAIL."
        )
    return "\n".join(lines)


def _parse_decisions(stdout: str) -> list[str]:
    return [
        line.split("DECISION:", 1)[1].strip()
        for line in stdout.splitlines()
        if "DECISION:" in line
    ]


def _parse_usage(stdout: str) -> tuple[float, int, int, list[dict[str, object]]]:
    """Read Omnigent's JSON session summary if present.

    Best-effort by design: upstream ships frequently, and a changed summary
    shape should cost us cost attribution, not the run.
    """
    cost, tin, tout = 0.0, 0, 0
    calls: list[dict[str, object]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else obj
        cost = float(usage.get("cost_usd", cost) or cost)
        tin = int(usage.get("tokens_in", tin) or tin)
        tout = int(usage.get("tokens_out", tout) or tout)
        if obj.get("type") == "tool_call":
            calls.append(
                {"tool": obj.get("tool"), "args": obj.get("args"), "path": obj.get("path")}
            )
    return cost, tin, tout, calls


def check_exit(node: TaskNode, worktree: Path) -> int | None:
    """Evaluate the node's machine-checkable exit condition."""
    kind = node.exit_check.kind
    if kind is ExitKind.COMMAND_EXIT_ZERO and node.exit_check.command:
        return _run(["bash", "-lc", node.exit_check.command], cwd=worktree, timeout=1800).returncode
    if kind is ExitKind.TESTS_PASS:
        return _run(["bash", "-lc", "make test"], cwd=worktree, timeout=1800).returncode
    if kind is ExitKind.BUILD_SUCCEEDS:
        return _run(["bash", "-lc", "make build"], cwd=worktree, timeout=1800).returncode
    if kind is ExitKind.SCHEMA_VALID:
        return _run(["bash", "-lc", "make validate"], cwd=worktree, timeout=600).returncode
    if kind is ExitKind.DIFF_NONEMPTY:
        return 0 if diff_stat(worktree) else 1
    return None


def diff_stat(worktree: Path) -> str | None:
    res = _run(["git", "diff", "--stat", "HEAD"], cwd=worktree)
    out = res.stdout.strip()
    return out or None


def execute(node: TaskNode, cfg: Config, run_id: str) -> NodeOutcome:
    """Run one node. Never raises for model-side failure; that goes in the outcome."""
    if node.harness is None:
        raise ExecutorError(f"node {node.id} was not routed")

    worktree = make_worktree(node, run_id)
    cmd = [
        "omnigent",
        "run",
        str(_agent_file(node)),
        "--harness",
        node.harness,
        "--cwd",
        str(worktree),
        "--non-interactive",
        "--json",
        "--prompt",
        _prompt(node),
        *_policy_flags(cfg, node),
    ]

    try:
        res = _run(cmd, timeout=7200)
    except subprocess.TimeoutExpired:
        return NodeOutcome(node.id, None, None, worktree, error="omnigent session timed out")

    cost, tin, tout, calls = _parse_usage(res.stdout)
    outcome = NodeOutcome(
        node_id=node.id,
        exit_code=None,
        diff_stat=diff_stat(worktree),
        worktree=worktree,
        cost_usd=cost,
        tokens_in=tin,
        tokens_out=tout,
        tool_calls=calls,
        decisions=_parse_decisions(res.stdout),
        stdout_tail=res.stdout[-4000:],
    )

    if res.returncode != 0:
        outcome.error = f"omnigent exited {res.returncode}: {res.stderr.strip()[:400]}"
        return outcome

    outcome.exit_code = check_exit(node, worktree)
    return outcome


def to_receipt(run_id: str, plan_hash: str, node: TaskNode, outcome: NodeOutcome) -> Receipt:
    status = "ok" if outcome.error is None and outcome.exit_code == 0 else "failed"
    return Receipt(
        run_id=run_id,
        plan_hash=plan_hash,
        node_id=node.id,
        role=node.role,
        vendor=node.vendor,
        harness=node.harness,
        tokens_in=outcome.tokens_in,
        tokens_out=outcome.tokens_out,
        cost_usd=outcome.cost_usd,
        tool_calls=outcome.tool_calls,
        decisions=outcome.decisions,
        diff_stat=outcome.diff_stat,
        worktree=str(outcome.worktree) if outcome.worktree else None,
        status=status,  # type: ignore[arg-type]
        error=outcome.error,
    )


def new_run_id() -> str:
    return uuid.uuid4().hex[:10]


def gc_worktrees() -> int:
    """Remove retained worktrees. Failed runs keep theirs so they stay inspectable."""
    if not WORKTREE_ROOT.exists():
        return 0
    removed = 0
    for path in sorted(WORKTREE_ROOT.iterdir()):
        if _run(["git", "worktree", "remove", "--force", str(path)]).returncode == 0:
            removed += 1
    _run(["git", "worktree", "prune"])
    return removed
