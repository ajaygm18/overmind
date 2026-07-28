"""Ground-truth file introspection.

The first cut of this repo had a hole it admitted in ADR-003: the linearity
gate decides what may run in parallel from the *declared* `writes` of each
node, and a planner that under-declares silently breaks that guarantee. A
declared file set is a promise by a language model. It is not evidence.

This module replaces the promise with a measurement. After a node runs, its
actual writes are read out of git, compared to what it declared, and the
parallel-safety decision the rewriter made *earlier* is re-proved against what
really happened.

That second part matters more than the first. Catching an out-of-scope write is
housekeeping. Discovering that two nodes which ran concurrently both touched
`src/auth.py` means the rewriter's disjointness proof was built on bad input,
and the run's output may contain two conflicting implicit decisions that no
merge tool can see. That finding is written to memory so the next plan for this
repository declares the file honestly.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from .models import GateResult, GateStatus, Plan, TaskNode


def _git(args: list[str], cwd: Path) -> str:
    res = subprocess.run(  # noqa: S603 - argv list, no shell
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    return res.stdout if res.returncode == 0 else ""


def _norm(path: str) -> str:
    return path.strip().lstrip("./")


def actual_writes(worktree: Path) -> set[str]:
    """Every path this worktree changed, tracked or not.

    `git diff --name-only HEAD` covers tracked edits and deletions.
    `git status --porcelain` is also needed because a brand-new file is
    untracked and therefore invisible to diff -- and new files are exactly how
    an agent quietly expands its own scope.
    """
    paths: set[str] = set()

    for line in _git(["diff", "--name-only", "HEAD"], worktree).splitlines():
        if line.strip():
            paths.add(_norm(line))

    for line in _git(["status", "--porcelain", "--untracked-files=all"], worktree).splitlines():
        if not line.strip():
            continue
        entry = line[3:] if len(line) > 3 else ""
        # Renames are reported as "old -> new"; both sides are real changes.
        if " -> " in entry:
            old, new = entry.split(" -> ", 1)
            paths.add(_norm(old))
            paths.add(_norm(new))
        elif entry.strip():
            paths.add(_norm(entry))

    return {p for p in paths if p and not p.startswith(".worktrees/")}


def covered_by(declared: list[str], path: str) -> bool:
    """Whether one actual path falls inside a declared set.

    Planners write file sets three ways and all three are legitimate: an exact
    path, a glob, or a directory standing for its contents. Treating a
    directory declaration as a literal filename would flag every honest node,
    so all three are accepted.
    """
    target = _norm(path)
    for raw in declared:
        entry = _norm(raw)
        if not entry:
            continue
        if target == entry or fnmatch(target, entry):
            return True
        if target.startswith(entry.rstrip("/") + "/"):
            return True
    return False


@dataclass
class WriteAudit:
    node_id: str
    declared: list[str]
    actual: set[str] = field(default_factory=set)

    @property
    def undeclared(self) -> set[str]:
        return {p for p in self.actual if not covered_by(self.declared, p)}

    @property
    def unused(self) -> set[str]:
        """Declared but never touched.

        Not a failure. It is over-declaration, which costs parallelism: the
        rewriter serialized work that could have run concurrently. Reported so
        the cost is visible rather than mysterious.
        """
        return {
            _norm(d)
            for d in self.declared
            if not any(covered_by([d], p) for p in self.actual)
        }


def audit_writes(node: TaskNode, worktree: Path | None) -> WriteAudit:
    actual = actual_writes(worktree) if worktree and worktree.exists() else set()
    return WriteAudit(node_id=node.id, declared=list(node.writes), actual=actual)


def declared_scope(audit: WriteAudit) -> GateResult:
    """Gate: did the node write only what it said it would?

    This is measured from git, so unlike the tool-call-based `action_trace` it
    cannot be defeated by a harness that reports its tool calls incompletely.
    """
    if audit.undeclared:
        return GateResult(
            gate="declared_scope",
            status=GateStatus.FAIL,
            mast_mode="disobey task specification",
            detail=(
                f"{audit.node_id} wrote {sorted(audit.undeclared)}, which it never declared. "
                "parallel safety for this level was computed without those paths."
            ),
        )
    return GateResult(
        gate="declared_scope",
        status=GateStatus.PASS,
        detail=f"{len(audit.actual)} path(s) written, all declared",
    )


def prove_disjoint(plan: Plan, audits: dict[str, WriteAudit]) -> list[GateResult]:
    """Re-prove, after the fact, that concurrent nodes really were independent.

    The rewriter's decision to allow concurrency was made from declared sets.
    Here the same question is asked of the measured sets. A collision means the
    two sessions may have made contradictory choices against the same file
    while neither could see the other -- Cognition's failure mode, and the one
    that produces output which merges cleanly and is still wrong.

    Only nodes that actually ran concurrently are compared. Nodes the rewriter
    serialized are excluded, because the second one legitimately saw and built
    on the first one's work.

    The gate refuses rather than passes when it cannot answer the question. It
    learns which nodes ran together from `plan.levels`; a plan that arrives with
    none, or with audited nodes missing from all of them, would otherwise
    produce a clean proof over zero comparisons -- a green result that means
    "not checked", which is precisely what this gate exists to prevent.
    """
    results: list[GateResult] = []

    scheduled = {nid for level in plan.levels for nid in level}

    if len(plan.nodes) > 1 and not scheduled:
        return [
            GateResult(
                gate="prove_disjoint",
                status=GateStatus.FAIL,
                mast_mode="conflicting implicit decisions",
                detail=(
                    f"plan has {len(plan.nodes)} nodes and no levels, so nothing is "
                    "known about which of them ran concurrently. the rewriter did "
                    "not run, and an unproven plan is not a disjoint one."
                ),
            )
        ]

    unscheduled = sorted(set(audits) - scheduled)
    if unscheduled and scheduled:
        return [
            GateResult(
                gate="prove_disjoint",
                status=GateStatus.FAIL,
                mast_mode="conflicting implicit decisions",
                detail=(
                    f"{unscheduled} produced write audits but appear in no level. "
                    "they ran, and the gate has no record of what ran beside them."
                ),
            )
        ]

    for level in plan.levels:
        present = [nid for nid in level if nid in audits]
        for i, left_id in enumerate(present):
            for right_id in present[i + 1 :]:
                left, right = audits[left_id], audits[right_id]
                if right_id in plan.node(left_id).depends_on:
                    continue
                if left_id in plan.node(right_id).depends_on:
                    continue
                overlap = left.actual & right.actual
                if overlap:
                    results.append(
                        GateResult(
                            gate="prove_disjoint",
                            status=GateStatus.FAIL,
                            mast_mode="conflicting implicit decisions",
                            detail=(
                                f"{left_id} and {right_id} ran concurrently and both wrote "
                                f"{sorted(overlap)}. the disjointness proof used declared "
                                "file sets and those were wrong."
                            ),
                        )
                    )

    if not results:
        results.append(
            GateResult(
                gate="prove_disjoint",
                status=GateStatus.PASS,
                detail="no concurrent node pair touched the same file",
            )
        )
    return results


def lesson(audit: WriteAudit) -> str | None:
    """A sentence worth persisting to memory for the next plan of this repo."""
    if not audit.undeclared:
        return None
    return (
        f"task {audit.node_id!r} also had to write {sorted(audit.undeclared)}; "
        "declare these paths up front when planning similar work here."
    )
