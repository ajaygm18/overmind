"""Resume a halted run.

The first cut printed `resume with: overmind resume <id>` and shipped no such
command -- and could not have, because nothing persisted the plan. Receipts
record what *happened*; they do not record what was *supposed* to happen. With
only receipts you cannot resume, because you no longer know the remaining work.

So a run now snapshots its rewritten plan, and resuming means: reload that
snapshot, confirm it still hashes to what the receipts reference, subtract the
nodes that already succeeded, and recompute the frontier.

What makes resume dangerous is not the bookkeeping, it is amnesia. A resumed
agent that cannot see the decisions the earlier half of the run already made
will re-decide them differently, which MAST files as lost context and is the
same failure as re-planning from scratch. So decisions are carried forward out
of the receipts and re-injected, and `gates.checkpoint_continuity` fails the
resume if they were dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import ReceiptConfig
from .models import Plan, Receipt
from .receipts import find, iter_receipts

PLAN_DIR = Path(".overmind/plans")


class ResumeError(Exception):
    pass


class PlanDiverged(ResumeError):
    """The snapshot no longer matches what the receipts were written against."""


def plan_path(run_id: str, directory: Path = PLAN_DIR) -> Path:
    return directory / f"{run_id}.json"


def save_plan(plan: Plan, run_id: str, directory: Path = PLAN_DIR) -> Path:
    """Snapshot the rewritten plan. Must be called before the first node runs.

    Saved after rewriting, not before: the executable plan is the rewritten one,
    including the verify nodes and serialization edges the rewriter added.
    Resuming from the pre-rewrite plan would silently drop verification.
    """
    if not plan.rewritten:
        raise ResumeError("refusing to snapshot a plan that has not been rewritten")
    directory.mkdir(parents=True, exist_ok=True)
    path = plan_path(run_id, directory)
    path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    return path


def load_plan(run_id: str, directory: Path = PLAN_DIR) -> Plan:
    path = plan_path(run_id, directory)
    if not path.exists():
        raise ResumeError(
            f"no plan snapshot for run {run_id} at {path}; "
            "runs started before snapshots existed cannot be resumed"
        )
    return Plan.model_validate_json(path.read_text(encoding="utf-8"))


def succeeded(receipts: list[Receipt]) -> set[str]:
    """Nodes that finished and passed every gate.

    A node whose receipt is 'ok' but which carries a blocking gate result is
    deliberately excluded: it produced output that failed inspection, so
    treating it as done would build on unverified work.
    """
    return {
        r.node_id
        for r in receipts
        if r.kind == "node"
        and r.status == "ok"
        and not any(g.blocking for g in r.gates)
    }


def attempted(receipts: list[Receipt]) -> set[str]:
    return {r.node_id for r in receipts if r.kind == "node"}


def carried_decisions(receipts: list[Receipt]) -> list[str]:
    """Every decision the earlier attempt declared, in order, de-duplicated."""
    seen: dict[str, None] = {}
    for r in receipts:
        for d in r.decisions:
            seen.setdefault(d.strip(), None)
    return [d for d in seen if d]


def spent(receipts: list[Receipt]) -> float:
    return round(sum(r.cost_usd for r in receipts), 4)


def check_hash(plan: Plan, receipts: list[Receipt]) -> None:
    """Refuse to resume against a plan the receipts do not describe."""
    referenced = {r.plan_hash for r in receipts if r.plan_hash}
    current = plan.content_hash()
    if referenced and current not in referenced:
        raise PlanDiverged(
            f"plan snapshot hashes to {current} but receipts reference "
            f"{sorted(referenced)}; start a fresh run rather than resuming"
        )


@dataclass
class ResumePoint:
    run_id: str
    plan: Plan
    plan_hash: str
    done: set[str] = field(default_factory=set)
    remaining_levels: list[list[str]] = field(default_factory=list)
    prior_decisions: list[str] = field(default_factory=list)
    already_spent: float = 0.0
    retrying: set[str] = field(default_factory=set)

    @property
    def remaining(self) -> list[str]:
        return [nid for level in self.remaining_levels for nid in level]

    @property
    def complete(self) -> bool:
        return not self.remaining

    def budget_left(self, budget_usd: float) -> float:
        """Resuming continues the original budget; it does not grant a new one."""
        return max(0.0, round(budget_usd - self.already_spent, 4))

    def summary(self) -> str:
        retry = f", retrying {sorted(self.retrying)}" if self.retrying else ""
        return (
            f"{len(self.done)} node(s) done, {len(self.remaining)} remaining"
            f"{retry}; ${self.already_spent:.4f} already spent"
        )


def plan_resume(
    run_id: str,
    cfg: ReceiptConfig,
    *,
    plan_dir: Path = PLAN_DIR,
) -> ResumePoint:
    """Work out exactly what is left to do, without running anything.

    A node is scheduled again if it did not succeed, even if it was attempted:
    the halt may have been mid-node, and its worktree was retained for
    inspection rather than reused. Levels are preserved so the concurrency the
    rewriter authorised still holds on the second half of the run.
    """
    receipts = list(iter_receipts(find(cfg, run_id)))
    if not receipts:
        raise ResumeError(f"run {run_id} has no receipts to resume from")

    plan = load_plan(run_id, plan_dir)
    check_hash(plan, receipts)

    done = succeeded(receipts)
    remaining_levels = [
        [nid for nid in level if nid not in done] for level in plan.levels
    ]

    return ResumePoint(
        run_id=run_id,
        plan=plan,
        plan_hash=plan.content_hash(),
        done=done,
        remaining_levels=[level for level in remaining_levels if level],
        prior_decisions=carried_decisions(receipts),
        already_spent=spent(receipts),
        retrying=attempted(receipts) - done,
    )
