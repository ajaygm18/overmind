"""The rewriter is the load-bearing part of this repo, so it is tested offline.

No model calls, no network, no upstream processes. These run in CI on every push.
"""

from __future__ import annotations

import pytest

from overmind import linearity
from overmind.models import ExitCheck, ExitKind, Plan, Role, TaskNode


def node(
    node_id: str,
    *,
    writes: list[str] | None = None,
    reads: list[str] | None = None,
    depends_on: list[str] | None = None,
    role: Role = Role.IMPLEMENTER,
    exit_kind: ExitKind = ExitKind.TESTS_PASS,
) -> TaskNode:
    return TaskNode(
        id=node_id,
        role=role,
        intent=f"do {node_id}",
        acceptance=f"{node_id} satisfies its stated requirement",
        reads=reads or [],
        writes=writes or [],
        depends_on=depends_on or [],
        exit_check=ExitCheck(kind=exit_kind),
    )


def plan_of(*nodes: TaskNode) -> Plan:
    return Plan(goal="test goal", nodes=list(nodes))


def test_disjoint_writes_stay_parallel() -> None:
    p = plan_of(node("a", writes=["src/a.py"]), node("b", writes=["src/b.py"]))
    linearity.rewrite(p, max_parallel=3)
    assert set(p.levels[0]) == {"a", "b"}


def test_write_write_conflict_is_serialized() -> None:
    p = plan_of(node("a", writes=["src/shared.py"]), node("b", writes=["src/shared.py"]))
    report = linearity.rewrite(p, max_parallel=3)
    assert report.serialized >= 1
    assert p.levels[0] == ["a"]
    assert "a" in p.node("b").depends_on


def test_write_read_conflict_is_serialized() -> None:
    """A reader observing a stale tree makes decisions against it. That is the
    conflicting-implicit-decisions failure, not a merge conflict."""
    p = plan_of(node("writer", writes=["src/api.py"]), node("reader", reads=["src/api.py"]))
    linearity.rewrite(p, max_parallel=3)
    assert "writer" in p.node("reader").depends_on


def test_every_writer_gets_a_verify_node() -> None:
    p = plan_of(node("a", writes=["src/a.py"]), node("b", writes=["src/b.py"]))
    report = linearity.rewrite(p, max_parallel=3)
    assert report.verifiers == 2
    for parent in ("a", "b"):
        verifier = p.node(f"{parent}__verify")
        assert verifier.role is Role.VERIFIER
        assert verifier.inspects == parent
        assert verifier.writes == []


def test_verification_is_not_inserted_twice() -> None:
    p = plan_of(node("a", writes=["src/a.py"]))
    linearity.rewrite(p, max_parallel=3)
    before = len(p.nodes)
    linearity.rewrite(p, max_parallel=3)
    assert len(p.nodes) == before


def test_downstream_waits_for_the_verifier_not_the_author() -> None:
    """Downstream work must not start from unverified output."""
    p = plan_of(
        node("a", writes=["src/a.py"]),
        node("b", writes=["src/b.py"], depends_on=["a"]),
    )
    linearity.rewrite(p, max_parallel=3)
    assert p.node("b").depends_on == ["a__verify"]


def test_width_is_capped_unless_wide() -> None:
    nodes = [node(f"n{i}", writes=[f"src/{i}.py"]) for i in range(6)]
    p = plan_of(*nodes)
    linearity.rewrite(p, max_parallel=2)
    assert max(len(level) for level in p.levels) <= 2

    wide = plan_of(*[node(f"m{i}", writes=[f"src/{i}.py"]) for i in range(6)])
    linearity.rewrite(wide, max_parallel=2, wide=True)
    assert max(len(level) for level in wide.levels) == 6


def test_model_assertion_exit_is_rejected() -> None:
    p = plan_of(node("a", writes=["src/a.py"], exit_kind=ExitKind.MODEL_ASSERTION))
    with pytest.raises(linearity.PlanInvalid, match="machine-checkable"):
        linearity.rewrite(p, max_parallel=3)


def test_cycles_are_rejected() -> None:
    p = plan_of(node("a", depends_on=["b"], writes=["a"]), node("b", depends_on=["a"], writes=["b"]))
    with pytest.raises(linearity.PlanInvalid, match="cycle"):
        linearity.rewrite(p, max_parallel=3)


def test_duplicate_ids_are_rejected() -> None:
    p = plan_of(node("a", writes=["x"]), node("a", writes=["y"]))
    with pytest.raises(linearity.PlanInvalid, match="duplicate"):
        linearity.rewrite(p, max_parallel=3)


def test_plan_hash_is_stable_and_order_independent() -> None:
    """Receipts reference the hash, so a replay must hash identically."""
    a = plan_of(node("a", writes=["src/a.py"]), node("b", writes=["src/b.py"]))
    b = plan_of(node("b", writes=["src/b.py"]), node("a", writes=["src/a.py"]))
    linearity.rewrite(a, max_parallel=3)
    linearity.rewrite(b, max_parallel=3)
    assert a.content_hash() == b.content_hash()


def test_serialization_order_is_deterministic() -> None:
    """Non-deterministic ordering would break plan hashing."""
    hashes = set()
    for _ in range(5):
        p = plan_of(
            node("z", writes=["src/shared.py"]),
            node("m", writes=["src/shared.py"]),
            node("a", writes=["src/shared.py"]),
        )
        linearity.rewrite(p, max_parallel=3)
        hashes.add(p.content_hash())
    assert len(hashes) == 1
