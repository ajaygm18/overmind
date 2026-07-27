"""The plan rewrite. This is the load-bearing module.

OMA returns a DAG. We do not execute it as returned. We rewrite it so that:

  1. write-conflicting siblings are serialized into a chain, so the second
     agent inherits the first's full context instead of guessing;
  2. every write node is followed by a verify node;
  3. parallelism never exceeds max_parallel without an explicit opt-in.

The reasoning behind (1) is Cognition's: when two agents touch the same file
the conflict is a *decision* conflict, not a textual one, and no merge tool can
see it. Anthropic's 90.2% multi-agent result holds for genuinely independent
work at ~15x the tokens, so independence is measured here rather than assumed.
"""

from __future__ import annotations

from collections import defaultdict

from .models import ExitCheck, ExitKind, Plan, Role, TaskNode


class PlanInvalid(Exception):
    """The plan cannot be made safe to execute."""


def _toposort(nodes: list[TaskNode]) -> list[list[str]]:
    """Kahn's algorithm, grouped into dependency levels."""
    indegree: dict[str, int] = {n.id: 0 for n in nodes}
    children: dict[str, list[str]] = defaultdict(list)
    ids = set(indegree)

    for n in nodes:
        for dep in n.depends_on:
            if dep not in ids:
                raise PlanInvalid(f"node {n.id!r} depends on unknown node {dep!r}")
            indegree[n.id] += 1
            children[dep].append(n.id)

    levels: list[list[str]] = []
    frontier = sorted(i for i, d in indegree.items() if d == 0)
    seen = 0
    while frontier:
        levels.append(frontier)
        seen += len(frontier)
        nxt: list[str] = []
        for node_id in frontier:
            for child in children[node_id]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    nxt.append(child)
        frontier = sorted(nxt)

    if seen != len(nodes):
        raise PlanInvalid("plan contains a dependency cycle")
    return levels


def _serialize_conflicts(plan: Plan, levels: list[list[str]]) -> int:
    """Add dependency edges between conflicting same-level nodes.

    Ordering within a conflict group is by node id, which is arbitrary but
    deterministic — important, because plan hashes must be stable across runs.
    Returns the number of edges added.
    """
    added = 0
    for level in levels:
        group = [plan.node(i) for i in level]
        for idx, node in enumerate(group):
            for earlier in group[:idx]:
                if node.conflicts_with(earlier) and earlier.id not in node.depends_on:
                    node.depends_on.append(earlier.id)
                    added += 1
    return added


def _cap_width(plan: Plan, levels: list[list[str]], max_parallel: int) -> int:
    """Chain the overflow of any level wider than max_parallel.

    Cheapest correct approach: keep the first max_parallel nodes concurrent and
    make each overflow node depend on the node max_parallel positions before
    it, which turns the tail into staggered waves rather than a single queue.
    """
    added = 0
    for level in levels:
        if len(level) <= max_parallel:
            continue
        for pos in range(max_parallel, len(level)):
            node = plan.node(level[pos])
            gate = level[pos - max_parallel]
            if gate not in node.depends_on:
                node.depends_on.append(gate)
                added += 1
    return added


def _verify_node_for(node: TaskNode) -> TaskNode:
    """A mandatory verify node. Structural, not configurable (MAST: verify_required).

    It inherits the author's exit check when that check is machine-checkable,
    and otherwise falls back to the test suite. It never inherits
    MODEL_ASSERTION, because that is not an exit condition.
    """
    check = (
        node.exit_check
        if node.exit_check.is_machine_checkable
        else ExitCheck(kind=ExitKind.TESTS_PASS)
    )
    return TaskNode(
        id=f"{node.id}__verify",
        role=Role.VERIFIER,
        intent=(
            f"Verify the output of {node.id!r} against its original acceptance "
            "criterion. Do not restate the criterion; compare against it verbatim."
        ),
        acceptance=node.acceptance,
        reads=sorted(set(node.writes) | set(node.reads)),
        writes=[],
        depends_on=[node.id],
        exit_check=check,
        synthesized=True,
        inspects=node.id,
    )


def _interleave_verification(plan: Plan) -> int:
    """Insert a verify node after every node that writes.

    Nodes that already depended on the writer are repointed at the verifier, so
    downstream work cannot start from unverified output.
    """
    writers = [n for n in plan.nodes if n.writes and n.role is not Role.VERIFIER]
    inserted: list[TaskNode] = []

    for node in writers:
        if any(n.inspects == node.id and n.role is Role.VERIFIER for n in plan.nodes):
            continue
        verifier = _verify_node_for(node)
        for other in plan.nodes:
            if other.id != node.id and node.id in other.depends_on:
                other.depends_on.remove(node.id)
                other.depends_on.append(verifier.id)
        inserted.append(verifier)

    plan.nodes.extend(inserted)
    return len(inserted)


def validate(plan: Plan) -> None:
    """Reject plans that cannot be gated, before any money is spent."""
    if not plan.nodes:
        raise PlanInvalid("plan has no nodes")

    ids = [n.id for n in plan.nodes]
    if len(set(ids)) != len(ids):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise PlanInvalid(f"duplicate node ids: {dupes}")

    for node in plan.nodes:
        if not node.exit_check.is_machine_checkable:
            raise PlanInvalid(
                f"node {node.id!r} has exit kind {node.exit_check.kind!r}, which is not "
                "machine-checkable. every node needs an exit a machine can check "
                "(MAST: unaware of termination conditions)."
            )
        if not node.acceptance.strip():
            raise PlanInvalid(f"node {node.id!r} has no acceptance criterion")
        if node.exit_check.kind is ExitKind.COMMAND_EXIT_ZERO and not node.exit_check.command:
            raise PlanInvalid(f"node {node.id!r} uses command_exit_zero with no command")


class RewriteReport(BaseException if False else object):  # noqa: N818 - plain data holder
    def __init__(self, serialized: int, capped: int, verifiers: int, levels: list[list[str]]):
        self.serialized = serialized
        self.capped = capped
        self.verifiers = verifiers
        self.levels = levels

    @property
    def widest(self) -> int:
        return max((len(lv) for lv in self.levels), default=0)

    def summary(self) -> str:
        return (
            f"{len(self.levels)} levels, widest {self.widest}; "
            f"+{self.serialized} conflict edges, +{self.capped} width edges, "
            f"+{self.verifiers} verify nodes"
        )


def rewrite(plan: Plan, max_parallel: int, wide: bool = False) -> RewriteReport:
    """Rewrite the plan in place and return what changed.

    Order matters: verification is inserted first so that verify nodes take part
    in conflict analysis and width capping like any other node.
    """
    validate(plan)

    verifiers = _interleave_verification(plan)
    levels = _toposort(plan.nodes)

    serialized = _serialize_conflicts(plan, levels)
    if serialized:
        levels = _toposort(plan.nodes)

    capped = 0
    if not wide:
        capped = _cap_width(plan, levels, max_parallel)
        if capped:
            levels = _toposort(plan.nodes)

    plan.levels = levels
    plan.rewritten = True
    return RewriteReport(serialized, capped, verifiers, levels)
