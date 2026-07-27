"""Tests for resume and worktree integration.

`overmind resume` was advertised by the first cut and did not exist. These
tests pin the behaviour that made it impossible before: nothing persisted the
plan, so the remaining work could not be recovered from receipts alone.

The git-touching parts of integrate.py are not exercised here; what is tested
is the ordering and reporting logic, which is where the correctness lives.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from overmind import integrate, linearity, resume
from overmind.config import ReceiptConfig
from overmind.models import (
    ExitCheck,
    ExitKind,
    GateResult,
    GateStatus,
    Plan,
    Receipt,
    Role,
    TaskNode,
)
from overmind.receipts import Ledger


def node(node_id: str, *, writes: list[str] | None = None, role: Role = Role.IMPLEMENTER) -> TaskNode:
    return TaskNode(
        id=node_id,
        role=role,
        intent=f"do {node_id}",
        acceptance=f"{node_id} is done",
        writes=writes or [],
        exit_check=ExitCheck(kind=ExitKind.TESTS_PASS),
    )


def rewritten_plan() -> Plan:
    p = Plan(goal="add oauth", nodes=[node("a", writes=["src/a.py"]), node("b", writes=["src/b.py"])])
    linearity.rewrite(p, max_parallel=3)
    return p


@pytest.fixture
def cfg(tmp_path: Path) -> ReceiptConfig:
    return ReceiptConfig(dir=str(tmp_path / "receipts"))


# -- plan snapshots ---------------------------------------------------------


def test_snapshot_round_trips(tmp_path: Path) -> None:
    p = rewritten_plan()
    resume.save_plan(p, "run1", tmp_path)
    loaded = resume.load_plan("run1", tmp_path)
    assert loaded.content_hash() == p.content_hash()
    assert loaded.levels == p.levels


def test_unrewritten_plans_are_refused(tmp_path: Path) -> None:
    """Snapshotting the pre-rewrite plan would silently drop the verify nodes
    the rewriter inserted, so a resume would skip verification."""
    raw = Plan(goal="g", nodes=[node("a", writes=["src/a.py"])])
    with pytest.raises(resume.ResumeError, match="not been rewritten"):
        resume.save_plan(raw, "run1", tmp_path)


def test_missing_snapshot_explains_itself(tmp_path: Path) -> None:
    with pytest.raises(resume.ResumeError, match="no plan snapshot"):
        resume.load_plan("nope", tmp_path)


# -- what counts as done ----------------------------------------------------


def ok(node_id: str, cost: float = 1.0, decisions: list[str] | None = None) -> Receipt:
    return Receipt(
        run_id="run1",
        plan_hash="h",
        node_id=node_id,
        cost_usd=cost,
        decisions=decisions or [],
        status="ok",
    )


def test_a_node_that_passed_but_failed_a_gate_is_not_done() -> None:
    """Otherwise a resume builds on output that failed inspection."""
    gated = ok("a")
    gated.gates = [
        GateResult(gate="declared_scope", status=GateStatus.FAIL, detail="wrote extra files")
    ]
    assert resume.succeeded([ok("b"), gated]) == {"b"}


def test_decisions_are_carried_forward_in_order_without_duplicates() -> None:
    receipts = [
        ok("a", decisions=["chose polling over websockets"]),
        ok("b", decisions=["chose polling over websockets", "added a 400 on expiry"]),
    ]
    assert resume.carried_decisions(receipts) == [
        "chose polling over websockets",
        "added a 400 on expiry",
    ]


# -- the resume point -------------------------------------------------------


def test_resume_point_subtracts_done_work_and_keeps_concurrency(
    tmp_path: Path, cfg: ReceiptConfig
) -> None:
    p = rewritten_plan()
    resume.save_plan(p, "run1", tmp_path)

    ledger = Ledger(cfg, "run1")
    done = Receipt(run_id="run1", plan_hash=p.content_hash(), node_id="a", cost_usd=2.5)
    ledger.append(done)

    point = resume.plan_resume("run1", cfg, plan_dir=tmp_path)
    assert point.done == {"a"}
    assert "a" not in point.remaining
    assert "a__verify" in point.remaining
    assert point.already_spent == 2.5
    assert not point.complete
    # Levels are preserved, so authorised concurrency still applies.
    assert all(isinstance(level, list) for level in point.remaining_levels)
    assert [] not in point.remaining_levels


def test_an_attempted_but_unfinished_node_is_retried(tmp_path: Path, cfg: ReceiptConfig) -> None:
    p = rewritten_plan()
    resume.save_plan(p, "run1", tmp_path)

    ledger = Ledger(cfg, "run1")
    ledger.append(
        Receipt(
            run_id="run1",
            plan_hash=p.content_hash(),
            node_id="a",
            status="failed",
            error="session timed out",
        )
    )

    point = resume.plan_resume("run1", cfg, plan_dir=tmp_path)
    assert "a" in point.retrying
    assert "a" in point.remaining


def test_resume_refuses_a_diverged_plan(tmp_path: Path, cfg: ReceiptConfig) -> None:
    """If the snapshot no longer matches what the receipts describe, resuming
    would run something other than what was approved."""
    p = rewritten_plan()
    resume.save_plan(p, "run1", tmp_path)
    Ledger(cfg, "run1").append(
        Receipt(run_id="run1", plan_hash="deadbeefdeadbeef", node_id="a")
    )
    with pytest.raises(resume.PlanDiverged, match="start a fresh run"):
        resume.plan_resume("run1", cfg, plan_dir=tmp_path)


def test_resume_continues_the_original_budget(tmp_path: Path, cfg: ReceiptConfig) -> None:
    p = rewritten_plan()
    resume.save_plan(p, "run1", tmp_path)
    Ledger(cfg, "run1").append(
        Receipt(run_id="run1", plan_hash=p.content_hash(), node_id="a", cost_usd=7.0)
    )
    point = resume.plan_resume("run1", cfg, plan_dir=tmp_path)
    assert point.budget_left(10.0) == 3.0
    assert point.budget_left(5.0) == 0.0  # never negative


def test_a_fully_finished_run_reports_complete(tmp_path: Path, cfg: ReceiptConfig) -> None:
    p = rewritten_plan()
    resume.save_plan(p, "run1", tmp_path)
    ledger = Ledger(cfg, "run1")
    for n in p.nodes:
        ledger.append(Receipt(run_id="run1", plan_hash=p.content_hash(), node_id=n.id))
    point = resume.plan_resume("run1", cfg, plan_dir=tmp_path)
    assert point.complete
    assert point.remaining == []


def test_resume_without_receipts_is_an_error(cfg: ReceiptConfig) -> None:
    with pytest.raises(FileNotFoundError):
        resume.plan_resume("never-ran", cfg)


# -- integration ------------------------------------------------------------


def test_branch_naming_matches_the_executor() -> None:
    """executor.make_worktree owns this format; a mismatch means merging the
    wrong branch or none at all."""
    assert integrate.branch_of("abc123", "impl") == "overmind/abc123/impl"


def test_merge_order_follows_plan_levels_and_skips_readonly_roles() -> None:
    p = rewritten_plan()
    receipts = [ok(n.id) for n in p.nodes]
    order = integrate._merge_order(p, receipts)
    assert order == ["a", "b"]  # verify nodes write nothing, so they are skipped


def test_failed_nodes_are_never_merged() -> None:
    p = rewritten_plan()
    failed = ok("a")
    failed.status = "failed"
    order = integrate._merge_order(p, [failed, ok("b")])
    assert order == ["b"]


def test_conflict_report_names_the_paths_and_the_rollback() -> None:
    report = integrate.IntegrationReport(base_branch="main", base_sha="a" * 40)
    report.conflict = integrate.Conflict(node_id="b", branch="overmind/r/b", paths=["src/shared.py"])
    report.rolled_back = True

    assert not report.ok
    assert "src/shared.py" in report.summary()
    assert "rolled back" in report.summary()

    gate = integrate.clean_merge(report)
    assert gate.blocking
    assert gate.mast_mode == "conflicting implicit decisions"


def test_a_run_left_dirty_says_so_loudly() -> None:
    """A half-integrated tree is worse than a failed run."""
    report = integrate.IntegrationReport(base_branch="main", base_sha="a" * 40)
    report.conflict = integrate.Conflict(node_id="b", branch="overmind/r/b", paths=[])
    report.rolled_back = False
    assert "LEFT DIRTY" in report.summary()


def test_clean_merge_passes_and_counts_empty_nodes() -> None:
    report = integrate.IntegrationReport(
        base_branch="main", base_sha="a" * 40, merged=["a", "b"], empty=["c"]
    )
    gate = integrate.clean_merge(report)
    assert not gate.blocking
    assert "merged 2" in gate.detail
    assert "1 produced no commit" in gate.detail
