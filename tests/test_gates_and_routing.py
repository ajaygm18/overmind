"""Gate and routing tests. Offline; no model calls.

The cross-vendor rule and the MAST gates are the two claims this repo makes
about reliability, so both are asserted rather than documented.
"""

from __future__ import annotations

import pytest

from overmind import gates, linearity, router
from overmind.config import Config, MemoryConfig, RunConfig
from overmind.memory import ALLOWED, RufloMemory, RufloToolNotAllowed
from overmind.models import ExitCheck, ExitKind, GateStatus, Plan, Receipt, Role, TaskNode


def cfg(vendors: dict[str, str] | None = None) -> Config:
    return Config(
        run=RunConfig(max_parallel=2, budget_usd=5.0),
        vendors=vendors or {"anthropic": "claude-sdk", "openai": "codex"},
        roles={"implementer": "anthropic", "verifier": "openai", "reviewer": "openai"},
    )


def writer(node_id: str = "impl") -> TaskNode:
    return TaskNode(
        id=node_id,
        role=Role.IMPLEMENTER,
        intent="add the device flow endpoint",
        acceptance="the device authorization endpoint returns a user_code and verification_uri",
        writes=["src/auth/device.py"],
        exit_check=ExitCheck(kind=ExitKind.TESTS_PASS),
    )


def routed_plan() -> Plan:
    p = Plan(goal="add oauth device flow", nodes=[writer()])
    linearity.rewrite(p, max_parallel=2)
    router.route(p, cfg())
    return p


# -- configuration ----------------------------------------------------------


def test_single_vendor_config_is_refused() -> None:
    """ADR-004: refusing beats silently degrading to same-vendor review."""
    with pytest.raises(ValueError, match="at least two vendors"):
        Config(vendors={"anthropic": "claude-sdk"})


def test_roles_cannot_reference_unconfigured_vendors() -> None:
    with pytest.raises(ValueError, match="unconfigured vendors"):
        Config(
            vendors={"anthropic": "claude-sdk", "openai": "codex"},
            roles={"implementer": "google"},
        )


# -- routing ----------------------------------------------------------------


def test_verifier_never_shares_the_authors_vendor() -> None:
    p = routed_plan()
    assert p.node("impl").vendor == "anthropic"
    assert p.node("impl__verify").vendor == "openai"


def test_router_overrides_a_role_preference_to_keep_vendors_distinct() -> None:
    """Preference loses to the diversity constraint, and does so silently but
    verifiably -- audit() would raise otherwise."""
    conf = cfg()
    conf.roles["verifier"] = "anthropic"  # same as the implementer
    p = Plan(goal="g", nodes=[writer()])
    linearity.rewrite(p, max_parallel=2)
    router.route(p, conf)
    assert p.node("impl__verify").vendor == "openai"


def test_audit_rejects_a_hand_tampered_same_vendor_pair() -> None:
    p = routed_plan()
    p.node("impl__verify").vendor = p.node("impl").vendor
    with pytest.raises(router.RoutingError, match="same vendor"):
        router.audit(p)


def test_inspectors_get_a_smaller_budget_share() -> None:
    p = routed_plan()
    router.distribute_budget(p, 10.0)
    assert p.node("impl").budget_usd > p.node("impl__verify").budget_usd
    assert sum(n.budget_usd or 0 for n in p.nodes) == pytest.approx(10.0, abs=0.01)


# -- gates ------------------------------------------------------------------


def test_plan_gates_pass_on_a_rewritten_plan() -> None:
    p = routed_plan()
    results = gates.plan_gates(p, ambiguity=0.1, threshold=0.65)
    assert [g for g in results if g.blocking] == []


def test_verify_required_fails_when_verification_is_stripped() -> None:
    p = routed_plan()
    p.nodes = [n for n in p.nodes if n.role is not Role.VERIFIER]
    failures = [g for g in gates.verify_required(p) if g.blocking]
    assert failures and failures[0].mast_mode == "no or incomplete verification"


def test_ambiguous_goal_halts_before_spending() -> None:
    result = gates.ambiguity_halt(0.8, 0.65)
    assert result.status is GateStatus.HALT
    assert result.mast_mode == "fail to ask for clarification"


def test_loop_detect_catches_identical_repetition() -> None:
    receipt = Receipt(
        run_id="r",
        plan_hash="h",
        node_id="impl",
        tool_calls=[{"tool": "grep", "args": {"q": "device"}} for _ in range(3)],
    )
    assert gates.loop_detect(receipt).blocking


def test_undeclared_writes_are_caught_after_the_fact() -> None:
    """The known hole in the linearity gate (ADR-003) surfaces here."""
    node = writer()
    receipt = Receipt(
        run_id="r",
        plan_hash="h",
        node_id=node.id,
        tool_calls=[{"tool": "write", "args": {}, "path": "src/auth/session.py"}],
    )
    result = gates.action_trace(node, receipt)
    assert result.blocking
    assert "src/auth/session.py" in result.detail


def test_code_change_without_a_declared_decision_fails() -> None:
    receipt = Receipt(run_id="r", plan_hash="h", node_id="impl", diff_stat="1 file changed")
    assert gates.decision_surface(receipt).blocking


def test_verifier_grading_its_own_paraphrase_is_caught() -> None:
    node = writer()
    drifted = gates.acceptance_drift(node, "the code looks fine and is well structured")
    faithful = gates.acceptance_drift(
        node, "device authorization endpoint returns user_code and verification_uri"
    )
    assert drifted.blocking
    assert not faithful.blocking


def test_silent_review_comment_fails() -> None:
    assert gates.review_ack(["1. handle expired codes"], []).blocking
    assert gates.review_ack(["1. handle expired codes"], ["rejected"]).blocking
    assert not gates.review_ack(
        ["1. handle expired codes"], ["applied: added a 400 on expiry with a test"]
    ).blocking


def test_premature_termination_needs_a_real_exit_code() -> None:
    node = writer()
    assert gates.exit_proof(node, None).blocking
    assert gates.exit_proof(node, 1).blocking
    assert not gates.exit_proof(node, 0).blocking


def test_resume_that_drops_prior_decisions_fails() -> None:
    assert gates.checkpoint_continuity(["chose polling"], []).blocking
    assert not gates.checkpoint_continuity(["chose polling"], ["chose polling", "added a test"]).blocking


# -- the Ruflo allowlist (ADR-002) -----------------------------------------


def test_ruflo_execution_tools_are_refused() -> None:
    mem = RufloMemory(MemoryConfig(enabled=False))
    for stubbed in ("agent_spawn", "task_assign", "workflow_execute", "neural_train"):
        with pytest.raises(RufloToolNotAllowed):
            mem.call(stubbed, {})


def test_terminal_execute_is_refused_even_though_it_works() -> None:
    """Two execution paths would mean one of them is unsandboxed."""
    mem = RufloMemory(MemoryConfig(enabled=False))
    with pytest.raises(RufloToolNotAllowed):
        mem.call("terminal_execute", {"command": "ls"})


def test_the_allowlist_is_exactly_the_four_memory_tools() -> None:
    assert ALLOWED == {
        "memory_store",
        "memory_search",
        "embeddings_generate",
        "session_save",
    }
