"""T04 and T06 tests: the gates that used to pass things they should not.

Every test here is written as the case that previously slipped through, so a
regression reads as a specific claim rather than 'a gate broke'. No network:
`restatement_fidelity` is called with cfg=None and uses the offline measure.
"""

from __future__ import annotations

from overmind import gates
from overmind.models import (
    ExitCheck,
    ExitKind,
    GateStatus,
    Plan,
    Receipt,
    Role,
    TaskNode,
)
from overmind.policy_export import INSPECTION_ROLES as POLICY_INSPECTION_ROLES


def node(**overrides: object) -> TaskNode:
    base: dict[str, object] = {
        "id": "n1",
        "role": Role.IMPLEMENTER,
        "intent": "add device-code auth",
        "acceptance": "the login endpoint returns 200 for a valid device code",
        "writes": ["src/auth.py"],
        "exit_check": ExitCheck(kind=ExitKind.TESTS_PASS),
    }
    base.update(overrides)
    return TaskNode.model_validate(base)


def receipt(**overrides: object) -> Receipt:
    base: dict[str, object] = {
        "run_id": "r1",
        "plan_hash": "h1",
        "node_id": "n1",
        "role": Role.IMPLEMENTER,
        "status": "ok",
    }
    base.update(overrides)
    return Receipt.model_validate(base)


def plan(nodes: list[TaskNode], levels: list[list[str]] | None = None) -> Plan:
    return Plan.model_validate(
        {
            "goal": "g",
            "nodes": [n.model_dump() for n in nodes],
            "levels": levels if levels is not None else [[n.id for n in nodes]],
        }
    )


# --------------------------------------------------------------------------
# T04: acceptance_drift
# --------------------------------------------------------------------------


def test_faithful_paraphrase_passes_even_with_different_words() -> None:
    """Word overlap failed this. It is the case the gate exists to allow."""
    restated = "the login endpoint returns a 200 response for a valid device code"
    result = gates.acceptance_drift(node(), restated)
    assert result.status is GateStatus.PASS


def test_drift_to_an_unrelated_criterion_fails() -> None:
    result = gates.acceptance_drift(node(), "the CSS renders correctly on mobile")
    assert result.status is GateStatus.FAIL
    assert "fidelity" in result.detail


def test_detail_reports_both_measures_so_a_bad_threshold_is_visible() -> None:
    detail = gates.acceptance_drift(node(), node().acceptance).detail
    assert "fidelity" in detail
    assert "word overlap" in detail


def test_verifier_that_restated_nothing_fails() -> None:
    assert gates.acceptance_drift(node(), "   ").status is GateStatus.FAIL


def test_empty_acceptance_fails_rather_than_dividing_by_zero() -> None:
    assert gates.acceptance_drift(node(acceptance=""), "anything").status is GateStatus.FAIL


def test_identical_restatement_scores_at_the_ceiling() -> None:
    result = gates.acceptance_drift(node(), node().acceptance)
    assert result.status is GateStatus.PASS
    assert "1.0" in result.detail


# --------------------------------------------------------------------------
# T06: spec_conformance
# --------------------------------------------------------------------------


def test_bare_pass_is_not_evidence() -> None:
    result = gates.spec_conformance(node(), "PASS")
    assert result.status is GateStatus.FAIL
    assert "no evidence" in result.detail


def test_pass_with_a_finding_is_accepted() -> None:
    verdict = "PASS - ran pytest tests/test_auth.py, 12 passed, endpoint returns 200"
    assert gates.spec_conformance(node(), verdict).status is GateStatus.PASS


def test_unparseable_verdict_is_not_a_pass() -> None:
    result = gates.spec_conformance(node(), "I think this looks broadly fine to me")
    assert result.status is GateStatus.FAIL
    assert "PASS or FAIL" in result.detail


def test_explicit_fail_is_reported_against_the_acceptance_text() -> None:
    result = gates.spec_conformance(node(), "FAIL - endpoint returns 500")
    assert result.status is GateStatus.FAIL
    assert "login endpoint" in result.detail


def test_a_verdict_starting_with_passive_is_not_read_as_pass() -> None:
    """Prefix matching on 'pass' would have accepted this."""
    result = gates.spec_conformance(node(), "passively reviewed, cannot confirm anything")
    assert result.status is GateStatus.FAIL


# --------------------------------------------------------------------------
# T06: role_scope
# --------------------------------------------------------------------------


def test_verifier_that_produced_a_diff_fails_even_with_allowed_tools() -> None:
    """The case that matters: a verifier able to edit can pass its own check."""
    result = gates.role_scope(
        node(role=Role.VERIFIER, writes=[]),
        allowed_tools={"sys_os_read", "sys_os_shell"},
        receipt=receipt(role=Role.VERIFIER, diff_stat=" src/auth.py | 4 +-"),
    )
    assert result.status is GateStatus.FAIL
    assert "must not produce edits" in result.detail


def test_verifier_that_only_read_and_ran_tests_passes() -> None:
    result = gates.role_scope(
        node(role=Role.VERIFIER, writes=[]),
        allowed_tools={"sys_os_read", "sys_os_shell"},
        receipt=receipt(
            role=Role.VERIFIER,
            tool_calls=[{"tool": "sys_os_shell", "args": {"command": "pytest"}}],
        ),
    )
    assert result.status is GateStatus.PASS


def test_implementer_producing_a_diff_is_fine() -> None:
    result = gates.role_scope(
        node(),
        allowed_tools={"sys_os_write"},
        receipt=receipt(diff_stat=" src/auth.py | 4 +-"),
    )
    assert result.status is GateStatus.PASS


def test_tool_outside_the_declared_set_still_fails() -> None:
    result = gates.role_scope(
        node(),
        allowed_tools={"sys_os_write"},
        receipt=receipt(tool_calls=[{"tool": "sys_os_shell", "args": {}}]),
    )
    assert result.status is GateStatus.FAIL
    assert "outside its declared set" in result.detail


def test_the_two_definitions_of_inspection_role_agree() -> None:
    """gates and policy_export each keep a list; drift between them is a hole."""
    from_gates = {str(getattr(r, "value", r)).lower() for r in gates.INSPECTION_ROLES}
    assert from_gates == set(POLICY_INSPECTION_ROLES)


# --------------------------------------------------------------------------
# T06: context_carry
# --------------------------------------------------------------------------


def test_transitively_ordered_conflict_passes() -> None:
    """The false positive: A -> B -> C, where A and C share a file."""
    a = node(id="a", writes=["src/auth.py"])
    b = node(id="b", writes=["src/other.py"], depends_on=["a"])
    c = node(id="c", writes=["src/auth.py"], depends_on=["b"])
    results = gates.context_carry(plan([a, b, c], [["a"], ["b"], ["c"]]))
    assert all(r.status is GateStatus.PASS for r in results)


def test_unordered_conflict_still_fails() -> None:
    a = node(id="a", writes=["src/auth.py"])
    b = node(id="b", writes=["src/auth.py"])
    results = gates.context_carry(plan([a, b]))
    assert any(r.status is GateStatus.FAIL for r in results)


def test_directly_ordered_conflict_passes() -> None:
    a = node(id="a", writes=["src/auth.py"])
    b = node(id="b", writes=["src/auth.py"], depends_on=["a"])
    results = gates.context_carry(plan([a, b], [["a"], ["b"]]))
    assert all(r.status is GateStatus.PASS for r in results)


def test_each_conflicting_pair_is_reported_once() -> None:
    a = node(id="a", writes=["src/auth.py"])
    b = node(id="b", writes=["src/auth.py"])
    results = gates.context_carry(plan([a, b]))
    assert len([r for r in results if r.status is GateStatus.FAIL]) == 1


def test_no_blanket_pass_is_emitted_alongside_a_failure() -> None:
    """A pass and a fail in the same list makes a report unreadable."""
    a = node(id="a", writes=["src/auth.py"])
    b = node(id="b", writes=["src/auth.py"])
    c = node(id="c", writes=["src/clean.py"])
    results = gates.context_carry(plan([a, b, c]))
    assert not any(r.status is GateStatus.PASS for r in results)
