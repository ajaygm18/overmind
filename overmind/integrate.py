"""Bring parallel worktrees back into one branch.

This closes a gap that made the parallelism in the first cut decorative. Each
node ran in its own `git worktree` on its own branch, gates passed, and then
the work sat there. A run could report success while the base branch was
unchanged.

None of the three upstreams solves this. Omnigent isolates sessions but does
not reunify them. OMA schedules tasks but is not git-aware. Ruflo has no
execution to integrate.

Two rules make the merge trustworthy:

1. **Sequential, in plan order.** Never an octopus merge. Merging one branch at
   a time in the order the rewriter established means that if two nodes did
   touch the same file, git surfaces it as a conflict at a known point instead
   of silently interleaving two agents' choices.

2. **A recorded base SHA, and rollback to it.** A half-integrated run is worse
   than a failed one, because the tree then contains part of a plan. On any
   conflict the merge aborts and the base branch resets to where it started.

A conflict here is a finding, not a crash: it is direct evidence that declared
file sets were wrong, which is exactly what `introspect.prove_disjoint` infers
and what gets written to memory for the next plan.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .models import GateResult, GateStatus, Plan, Receipt


class IntegrationError(Exception):
    pass


def _git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - argv list, no shell
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )


def branch_of(run_id: str, node_id: str) -> str:
    """Must match executor.make_worktree, which owns the naming."""
    return f"overmind/{run_id}/{node_id}"


def current_branch() -> str:
    res = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    if res.returncode != 0:
        raise IntegrationError(f"not a git repository: {res.stderr.strip()[:200]}")
    return res.stdout.strip()


def head_sha(ref: str = "HEAD") -> str:
    res = _git(["rev-parse", ref])
    if res.returncode != 0:
        raise IntegrationError(f"cannot resolve {ref}: {res.stderr.strip()[:200]}")
    return res.stdout.strip()


def is_dirty() -> bool:
    return bool(_git(["status", "--porcelain"]).stdout.strip())


def commit_worktree(worktree: Path, node_id: str) -> str | None:
    """Commit whatever the session left behind, so it can be merged.

    Agents finish with uncommitted changes far more often than not. Returns None
    when the node produced nothing, which is normal for read-only roles.
    """
    if not worktree.exists():
        return None
    if not _git(["status", "--porcelain", "--untracked-files=all"], worktree).stdout.strip():
        return None

    if _git(["add", "-A"], worktree).returncode != 0:
        raise IntegrationError(f"git add failed in {worktree}")

    res = _git(["commit", "-m", f"overmind: {node_id}"], worktree)
    if res.returncode != 0 and "nothing to commit" not in res.stdout:
        raise IntegrationError(f"git commit failed for {node_id}: {res.stderr.strip()[:200]}")

    return _git(["rev-parse", "HEAD"], worktree).stdout.strip() or None


@dataclass
class Conflict:
    node_id: str
    branch: str
    paths: list[str]


@dataclass
class IntegrationReport:
    base_branch: str
    base_sha: str
    merged: list[str] = field(default_factory=list)
    empty: list[str] = field(default_factory=list)
    conflict: Conflict | None = None
    rolled_back: bool = False

    @property
    def ok(self) -> bool:
        return self.conflict is None

    def summary(self) -> str:
        if self.conflict:
            state = "rolled back to " + self.base_sha[:8] if self.rolled_back else "LEFT DIRTY"
            return (
                f"conflict merging {self.conflict.node_id} on "
                f"{', '.join(self.conflict.paths) or 'unknown paths'}; {state}"
            )
        return (
            f"merged {len(self.merged)} branch(es) into {self.base_branch}"
            + (f", {len(self.empty)} produced no commit" if self.empty else "")
        )


def _conflicted_paths() -> list[str]:
    res = _git(["diff", "--name-only", "--diff-filter=U"])
    return sorted({line.strip() for line in res.stdout.splitlines() if line.strip()})


def _merge_order(plan: Plan, receipts: list[Receipt]) -> list[str]:
    """Plan order, restricted to nodes that ran and passed.

    Verify and review nodes are skipped: they are read-only by construction, so
    they have nothing to contribute to the tree.
    """
    ok = {r.node_id for r in receipts if r.kind == "node" and r.status == "ok"}
    ordered: list[str] = []
    for level in plan.levels:
        for node_id in level:
            if node_id in ok and plan.node(node_id).writes:
                ordered.append(node_id)
    return ordered


def integrate(
    plan: Plan,
    run_id: str,
    receipts: list[Receipt],
    *,
    allow_dirty: bool = False,
) -> IntegrationReport:
    """Merge every successful writing node's branch, in plan order.

    Refuses to start on a dirty tree unless told otherwise: mixing the
    operator's uncommitted work into an agent run makes the rollback
    unsafe, because reset --hard would destroy work Overmind never created.
    """
    if is_dirty() and not allow_dirty:
        raise IntegrationError(
            "working tree has uncommitted changes; commit or stash first "
            "(rollback would otherwise discard your work)"
        )

    base_branch = current_branch()
    report = IntegrationReport(base_branch=base_branch, base_sha=head_sha())

    worktrees = {
        r.node_id: Path(r.worktree)
        for r in receipts
        if r.kind == "node" and r.worktree
    }

    for node_id in _merge_order(plan, receipts):
        worktree = worktrees.get(node_id)
        if worktree is None:
            report.empty.append(node_id)
            continue

        if commit_worktree(worktree, node_id) is None:
            report.empty.append(node_id)
            continue

        branch = branch_of(run_id, node_id)
        res = _git(["merge", "--no-ff", "--no-edit", branch])

        if res.returncode != 0:
            report.conflict = Conflict(
                node_id=node_id, branch=branch, paths=_conflicted_paths()
            )
            _git(["merge", "--abort"])
            report.rolled_back = rollback(report.base_sha)
            return report

        report.merged.append(node_id)

    return report


def rollback(base_sha: str) -> bool:
    """Reset the base branch to where integration began."""
    return _git(["reset", "--hard", base_sha]).returncode == 0


def clean_merge(report: IntegrationReport) -> GateResult:
    """Gate: the run's output actually landed on one branch.

    A conflict is reported as the same MAST mode `prove_disjoint` uses. Both are
    symptoms of one cause: two agents were told they were independent and were
    not. Git found it textually; prove_disjoint finds it even when the text
    merges cleanly.
    """
    if report.conflict is None:
        return GateResult(
            gate="clean_merge", status=GateStatus.PASS, detail=report.summary()
        )
    return GateResult(
        gate="clean_merge",
        status=GateStatus.FAIL,
        mast_mode="conflicting implicit decisions",
        detail=report.summary(),
    )
