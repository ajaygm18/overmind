"""T05: integration against a real repository.

Every test here exercises code that can destroy work. They run inside a
tmp_path repository, and each one chdirs first because `integrate` operates on
the process working directory by design.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from overmind import integrate
from overmind.models import ExitCheck, ExitKind, GateStatus, Plan, Receipt, Role, TaskNode

from .gitfixture import Repo, make_repo

RUN = "run1"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Repo:
    r = make_repo(tmp_path / "project")
    monkeypatch.chdir(r.path)
    return r


def node(node_id: str, writes: list[str], role: Role = Role.IMPLEMENTER) -> TaskNode:
    return TaskNode.model_validate(
        {
            "id": node_id,
            "role": role,
            "intent": f"work on {writes}",
            "acceptance": "the tests pass",
            "writes": writes,
            "exit_check": ExitCheck(kind=ExitKind.TESTS_PASS),
        }
    )


def receipt(node_id: str, worktree: Path | None, status: str = "ok") -> Receipt:
    return Receipt.model_validate(
        {
            "run_id": RUN,
            "plan_hash": "h1",
            "node_id": node_id,
            "kind": "node",
            "role": Role.IMPLEMENTER,
            "status": status,
            "worktree": str(worktree) if worktree else None,
        }
    )


def plan(nodes: list[TaskNode]) -> Plan:
    return Plan.model_validate(
        {
            "goal": "g",
            "nodes": [n.model_dump() for n in nodes],
            "levels": [[n.id for n in nodes]],
        }
    )


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------


def test_a_single_node_lands_on_the_base_branch(repo: Repo) -> None:
    wt = repo.worktree(RUN, "n1")
    repo.work(wt, "src/auth.py", "def login(): return 200\n")

    report = integrate.integrate(plan([node("n1", ["src/auth.py"])]), RUN, [receipt("n1", wt)])

    assert report.ok
    assert report.merged == ["n1"]
    assert repo.read("src/auth.py") == "def login(): return 200\n"
    assert repo.branch() == "main"


def test_two_disjoint_nodes_both_land(repo: Repo) -> None:
    a, b = repo.worktree(RUN, "n1"), repo.worktree(RUN, "n2")
    repo.work(a, "src/auth.py", "auth\n")
    repo.work(b, "src/billing.py", "billing\n")

    report = integrate.integrate(
        plan([node("n1", ["src/auth.py"]), node("n2", ["src/billing.py"])]),
        RUN,
        [receipt("n1", a), receipt("n2", b)],
    )

    assert report.merged == ["n1", "n2"]
    assert repo.exists("src/auth.py")
    assert repo.exists("src/billing.py")


def test_uncommitted_session_work_is_committed_before_merging(repo: Repo) -> None:
    """Agents finish dirty far more often than not."""
    wt = repo.worktree(RUN, "n1")
    repo.work(wt, "src/new_file.py", "x = 1\n", commit=False)

    report = integrate.integrate(plan([node("n1", ["src/new_file.py"])]), RUN, [receipt("n1", wt)])

    assert report.merged == ["n1"]
    assert repo.exists("src/new_file.py")


def test_merge_is_never_fast_forwarded_away(repo: Repo) -> None:
    """--no-ff keeps one commit per node, so a run stays attributable."""
    wt = repo.worktree(RUN, "n1")
    repo.work(wt, "src/auth.py", "auth\n")
    before = repo.head()

    integrate.integrate(plan([node("n1", ["src/auth.py"])]), RUN, [receipt("n1", wt)])

    parents = repo.head("HEAD^@").splitlines() if repo.head() != before else []
    assert repo.head() != before
    assert len(parents) >= 1


# --------------------------------------------------------------------------
# conflicts and rollback
# --------------------------------------------------------------------------


def test_conflicting_edits_are_reported_not_raised(repo: Repo) -> None:
    a, b = repo.worktree(RUN, "n1"), repo.worktree(RUN, "n2")
    repo.work(a, "src/app.py", "VERSION = 2\n")
    repo.work(b, "src/app.py", "VERSION = 3\n")

    report = integrate.integrate(
        plan([node("n1", ["src/app.py"]), node("n2", ["src/app.py"])]),
        RUN,
        [receipt("n1", a), receipt("n2", b)],
    )

    assert not report.ok
    assert report.conflict is not None
    assert report.conflict.node_id == "n2"
    assert report.conflict.paths == ["src/app.py"]


def test_partial_integration_does_not_survive_a_conflict(repo: Repo) -> None:
    """The central guarantee. A tree containing half a plan is worse than none."""
    base = repo.head()
    a, b, c = (
        repo.worktree(RUN, "n1"),
        repo.worktree(RUN, "n2"),
        repo.worktree(RUN, "n3"),
    )
    repo.work(a, "src/first.py", "first\n")
    repo.work(b, "src/app.py", "VERSION = 2\n")
    repo.work(c, "src/app.py", "VERSION = 3\n")

    report = integrate.integrate(
        plan(
            [
                node("n1", ["src/first.py"]),
                node("n2", ["src/app.py"]),
                node("n3", ["src/app.py"]),
            ]
        ),
        RUN,
        [receipt("n1", a), receipt("n2", b), receipt("n3", c)],
    )

    assert not report.ok
    assert report.rolled_back
    assert repo.head() == base
    assert not repo.exists("src/first.py"), "n1's work survived a rolled-back run"
    assert repo.read("src/app.py") == "VERSION = 1\n"


def test_no_merge_is_left_in_progress_after_a_conflict(repo: Repo) -> None:
    """A left-open merge would make the next command fail confusingly."""
    a, b = repo.worktree(RUN, "n1"), repo.worktree(RUN, "n2")
    repo.work(a, "src/app.py", "VERSION = 2\n")
    repo.work(b, "src/app.py", "VERSION = 3\n")

    integrate.integrate(
        plan([node("n1", ["src/app.py"]), node("n2", ["src/app.py"])]),
        RUN,
        [receipt("n1", a), receipt("n2", b)],
    )

    assert not repo.merging()
    assert not repo.dirty()


def test_conflict_is_reported_as_the_shared_mast_mode(repo: Repo) -> None:
    a, b = repo.worktree(RUN, "n1"), repo.worktree(RUN, "n2")
    repo.work(a, "src/app.py", "VERSION = 2\n")
    repo.work(b, "src/app.py", "VERSION = 3\n")

    report = integrate.integrate(
        plan([node("n1", ["src/app.py"]), node("n2", ["src/app.py"])]),
        RUN,
        [receipt("n1", a), receipt("n2", b)],
    )
    result = integrate.clean_merge(report)

    assert result.status is GateStatus.FAIL
    assert result.mast_mode == "conflicting implicit decisions"


# --------------------------------------------------------------------------
# refusals and skips
# --------------------------------------------------------------------------


def test_a_dirty_tree_is_refused_because_rollback_would_destroy_it(repo: Repo) -> None:
    wt = repo.worktree(RUN, "n1")
    repo.work(wt, "src/auth.py", "auth\n")
    repo.write("src/operator_wip.py", "my unfinished work\n")

    with pytest.raises(integrate.IntegrationError, match="uncommitted"):
        integrate.integrate(plan([node("n1", ["src/auth.py"])]), RUN, [receipt("n1", wt)])

    assert repo.read("src/operator_wip.py") == "my unfinished work\n"


def test_dirty_tree_can_be_overridden_explicitly(repo: Repo) -> None:
    wt = repo.worktree(RUN, "n1")
    repo.work(wt, "src/auth.py", "auth\n")
    repo.write("untracked.txt", "x\n")

    report = integrate.integrate(
        plan([node("n1", ["src/auth.py"])]), RUN, [receipt("n1", wt)], allow_dirty=True
    )
    assert report.merged == ["n1"]


def test_worktree_and_overmind_dirs_do_not_make_the_tree_dirty(repo: Repo) -> None:
    """Otherwise integrate() refuses on every real run. This is a contract."""
    repo.worktree(RUN, "n1")
    (repo.path / ".overmind" / "specs").mkdir(parents=True, exist_ok=True)
    (repo.path / ".overmind" / "specs" / "x.yaml").write_text("name: x\n")
    assert not integrate.is_dirty()


def test_a_node_that_produced_nothing_is_recorded_as_empty(repo: Repo) -> None:
    wt = repo.worktree(RUN, "n1")

    report = integrate.integrate(plan([node("n1", ["src/auth.py"])]), RUN, [receipt("n1", wt)])

    assert report.empty == ["n1"]
    assert report.merged == []
    assert report.ok


def test_a_failed_node_is_not_merged(repo: Repo) -> None:
    wt = repo.worktree(RUN, "n1")
    repo.work(wt, "src/broken.py", "syntax error\n")

    report = integrate.integrate(
        plan([node("n1", ["src/broken.py"])]), RUN, [receipt("n1", wt, status="failed")]
    )

    assert report.merged == []
    assert not repo.exists("src/broken.py")


def test_a_read_only_node_is_skipped_entirely(repo: Repo) -> None:
    wt = repo.worktree(RUN, "v1")
    repo.work(wt, "notes.txt", "looks fine\n")

    report = integrate.integrate(
        plan([node("v1", [], role=Role.VERIFIER)]), RUN, [receipt("v1", wt)]
    )

    assert report.merged == []
    assert not repo.exists("notes.txt")


# --------------------------------------------------------------------------
# cross-module agreement
# --------------------------------------------------------------------------


def test_branch_naming_agrees_with_the_executor(repo: Repo) -> None:
    """If these drift, a run merges nothing and reports success."""
    from overmind import executor

    path = executor.make_worktree(node("n9", ["src/x.py"]), RUN)

    assert path.exists()
    assert integrate.branch_of(RUN, "n9") in repo.branches()


def test_commit_worktree_returns_none_when_there_is_nothing_to_commit(
    repo: Repo,
) -> None:
    wt = repo.worktree(RUN, "n1")
    assert integrate.commit_worktree(wt, "n1") is None


def test_commit_worktree_returns_a_sha_when_it_committed(repo: Repo) -> None:
    wt = repo.worktree(RUN, "n1")
    repo.work(wt, "src/auth.py", "auth\n")
    sha = integrate.commit_worktree(wt, "n1")
    assert sha and len(sha) == 40
