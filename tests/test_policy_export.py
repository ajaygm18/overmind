"""T01 tests: the compiled policy block, and the handlers it points at.

These never start Omnigent. They drive the handlers with the event shape
documented in `POLICIES.md`, which is the contract that actually matters -- if
upstream changes that shape, these tests are what should fail.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from overmind.config import Config
from overmind.policies import runtime
from overmind.policy_export import (
    OVERMIND_DIR,
    OVERMIND_LOOP,
    OVERMIND_ROLE,
    OVERMIND_SCOPE,
    UPSTREAM_COST_BUDGET,
    UPSTREAM_MAX_TOOL_CALLS,
    compile_policies,
    interactive_policies,
    may_write,
)

WORKTREE = "/tmp/wt/run1/n1"


@dataclass
class FakeNode:
    """Duck-types the slice of TaskNode the compiler declares it needs."""

    id: str = "n1"
    role: str = "implementer"
    writes: list[str] = field(default_factory=lambda: ["src/auth.py"])
    budget_usd: float | None = 2.0


def cfg() -> Config:
    return Config.model_validate(
        {"vendors": {"anthropic": "claude-sdk", "openai": "codex"}}
    )


def call(name: str, **arguments: Any) -> dict[str, Any]:
    return {
        "type": "tool_call",
        "target": name,
        "data": {"name": name, "arguments": arguments},
        "context": {"usage": {"total_cost_usd": 0.0}},
        "session_state": {},
        "request_data": None,
    }


# --------------------------------------------------------------------------
# compile_policies
# --------------------------------------------------------------------------


def test_every_declaration_matches_the_documented_shape() -> None:
    allowed = {"type", "handler", "factory_params"}
    for name, decl in compile_policies(FakeNode(), cfg(), WORKTREE).items():
        assert set(decl) <= allowed, f"{name} has undocumented keys"
        assert decl["type"] == "function"
        assert isinstance(decl["handler"], str) and "." in decl["handler"]


def test_every_overmind_handler_actually_exists() -> None:
    """A dotted path that does not resolve is a policy that fails at startup."""
    for decl in compile_policies(FakeNode(), cfg(), WORKTREE).values():
        handler = decl["handler"]
        if not handler.startswith("overmind."):
            continue
        assert hasattr(runtime, handler.rsplit(".", 1)[1])


def test_only_documented_upstream_handlers_are_emitted() -> None:
    upstream = {
        d["handler"]
        for d in compile_policies(FakeNode(), cfg(), WORKTREE).values()
        if d["handler"].startswith("omnigent.")
    }
    assert upstream <= {UPSTREAM_MAX_TOOL_CALLS, UPSTREAM_COST_BUDGET}
    assert not any("block_working_dir_changes" in h for h in upstream)


def test_confinement_is_pinned_to_this_worktree_and_nothing_broader() -> None:
    policies = compile_policies(FakeNode(), cfg(), WORKTREE)
    params = policies["overmind_worktree_confinement"]["factory_params"]
    assert params["allowed_dirs"] == [WORKTREE]
    assert "." not in params["allowed_dirs"]
    assert "/" not in params["allowed_dirs"]


def test_confinement_and_scope_are_always_present() -> None:
    for role in ("implementer", "reviewer", "verifier", "researcher"):
        policies = compile_policies(FakeNode(role=role), cfg(), WORKTREE)
        handlers = {d["handler"] for d in policies.values()}
        assert OVERMIND_DIR in handlers
        assert OVERMIND_SCOPE in handlers
        assert OVERMIND_ROLE in handlers
        assert OVERMIND_LOOP in handlers


def test_cheap_checks_are_declared_before_the_expensive_one() -> None:
    """DENY short-circuits, so ordering is a performance contract."""
    order = list(compile_policies(FakeNode(), cfg(), WORKTREE))
    assert order.index("overmind_declared_scope") < order.index("overmind_loop_guard")


def test_budget_is_omitted_when_unallocated() -> None:
    policies = compile_policies(FakeNode(budget_usd=None), cfg(), WORKTREE)
    assert "overmind_node_budget" not in policies


def test_budget_ask_threshold_is_below_the_hard_limit() -> None:
    """POLICIES.md requires each ask threshold to be < max_cost_usd."""
    params = compile_policies(FakeNode(budget_usd=2.0), cfg(), WORKTREE)[
        "overmind_node_budget"
    ]["factory_params"]
    assert all(t < params["max_cost_usd"] for t in params["ask_thresholds_usd"])


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("implementer", True),
        ("IMPLEMENTER", True),
        ("reviewer", False),
        ("verifier", False),
        ("researcher", False),
        ("planner", False),
    ],
)
def test_only_producing_roles_may_write(role: str, expected: bool) -> None:
    assert may_write(FakeNode(role=role)) is expected


def test_supervised_approval_is_opt_in_only() -> None:
    assert "overmind_supervised" not in compile_policies(FakeNode(), cfg(), WORKTREE)
    assert "overmind_supervised" in interactive_policies()


# --------------------------------------------------------------------------
# scope_guard
# --------------------------------------------------------------------------


def test_scope_allows_a_declared_write() -> None:
    guard = runtime.scope_guard(["src/auth.py"])
    assert guard(call("sys_os_write", path="src/auth.py"))["result"] == "ALLOW"


def test_scope_denies_an_undeclared_write() -> None:
    guard = runtime.scope_guard(["src/auth.py"])
    verdict = guard(call("sys_os_edit", path="src/billing.py"))
    assert verdict["result"] == "DENY"
    assert "billing" in verdict["reason"]


def test_scope_accepts_directory_and_glob_declarations() -> None:
    """Shares introspect.covered_by, so all three declaration styles work."""
    assert runtime.scope_guard(["src/"])(
        call("sys_os_write", path="src/deep/nested.py")
    )["result"] == "ALLOW"
    assert runtime.scope_guard(["tests/test_*.py"])(
        call("sys_os_write", path="tests/test_auth.py")
    )["result"] == "ALLOW"


def test_scope_reads_alternate_path_argument_names() -> None:
    """Harnesses disagree on the key; the guard must not be bypassable by it."""
    for key in ("path", "file_path", "file", "target_file", "filename"):
        verdict = runtime.scope_guard(["src/auth.py"])(
            call("sys_os_write", **{key: "secrets.env"})
        )
        assert verdict["result"] == "DENY", key


def test_scope_denies_all_writes_when_nothing_was_declared() -> None:
    verdict = runtime.scope_guard([])(call("sys_os_write", path="anything.py"))
    assert verdict["result"] == "DENY"


def test_scope_asks_rather_than_guesses_on_a_pathless_write() -> None:
    verdict = runtime.scope_guard(["src/auth.py"])(call("sys_os_write"))
    assert verdict["result"] == "ASK"


def test_scope_asks_on_shell_redirection_and_allows_plain_reads() -> None:
    guard = runtime.scope_guard(["src/auth.py"], ask_on_shell=True)
    assert guard(call("sys_os_shell", command="echo x > out.txt"))["result"] == "ASK"
    assert guard(call("sys_os_shell", command="pytest -q"))["result"] == "ALLOW"


def test_scope_can_be_told_not_to_ask_for_unattended_runs() -> None:
    guard = runtime.scope_guard(["src/auth.py"], ask_on_shell=False)
    assert guard(call("sys_os_shell", command="rm -rf build")) is None


def test_scope_abstains_on_phases_it_does_not_judge() -> None:
    guard = runtime.scope_guard(["src/auth.py"])
    assert guard({"type": "request", "data": {}}) is None
    assert guard({"type": "response", "data": {}}) is None
    assert guard(call("sys_os_read", path="whatever.py")) is None


# --------------------------------------------------------------------------
# dir_guard
# --------------------------------------------------------------------------


def test_dir_guard_denies_worktree_and_directory_escapes() -> None:
    guard = runtime.dir_guard([WORKTREE])
    for command in (
        "git worktree add ../elsewhere",
        "cd /etc && cat passwd",
        "pushd /tmp",
        "git clone https://example.com/x",
    ):
        assert guard(call("sys_os_shell", command=command))["result"] == "DENY", command


def test_dir_guard_allows_movement_inside_the_worktree() -> None:
    guard = runtime.dir_guard([WORKTREE])
    verdict = guard(call("sys_os_shell", command=f"cd {WORKTREE}/src && ls"))
    assert verdict["result"] == "ALLOW"


def test_dir_guard_ignores_non_shell_tools() -> None:
    assert runtime.dir_guard([WORKTREE])(call("sys_os_read", path="a.py")) is None


# --------------------------------------------------------------------------
# role_guard
# --------------------------------------------------------------------------


def test_role_guard_denies_a_reviewer_that_writes_code() -> None:
    verdict = runtime.role_guard("reviewer", may_write=False)(
        call("sys_os_edit", path="src/auth.py")
    )
    assert verdict["result"] == "DENY"
    assert "another role" in verdict["reason"]


def test_role_guard_abstains_entirely_for_producing_roles() -> None:
    guard = runtime.role_guard("implementer", may_write=True)
    assert guard(call("sys_os_edit", path="src/auth.py")) is None


def test_role_guard_lets_inspectors_read_and_run_tests() -> None:
    guard = runtime.role_guard("verifier", may_write=False)
    assert guard(call("sys_os_read", path="src/auth.py")) is None
    assert guard(call("sys_os_shell", command="pytest -q")) is None


# --------------------------------------------------------------------------
# loop_guard
# --------------------------------------------------------------------------


def with_state(event: dict[str, Any], history: list[str]) -> dict[str, Any]:
    return {**event, "session_state": {"overmind_recent_calls": history}}


def test_loop_guard_records_history_bounded() -> None:
    guard = runtime.loop_guard(history=4)
    verdict = guard(with_state(call("grep", q="a"), ["x", "y", "z", "w"]))
    update = verdict["state_updates"][0]
    assert update["action"] == "set"
    assert len(update["value"]) == 4


def test_loop_guard_denies_the_third_rephrased_repeat() -> None:
    """The documented motivating case: device_code, device code, device-code."""
    guard = runtime.loop_guard()
    history = ["grep  device_code", "grep  device code"]
    verdict = guard(with_state(call("grep", pattern="device-code"), history))
    assert verdict["result"] == "DENY"
    assert "in a row" in verdict["reason"]


def test_loop_guard_allows_genuine_progress() -> None:
    guard = runtime.loop_guard()
    history = ["read  src/auth.py", "pytest  tests/test_auth.py"]
    verdict = guard(with_state(call("sys_os_edit", path="src/auth.py"), history))
    assert verdict["result"] == "ALLOW"


def test_loop_guard_still_catches_byte_identical_repeats() -> None:
    """The semantic measure must subsume the exact-match gate it replaces."""
    guard = runtime.loop_guard()
    same = "grep  device_code"
    verdict = guard(with_state(call("grep", pattern="device_code"), [same, same]))
    assert verdict["result"] == "DENY"


def test_loop_guard_does_not_redeny_an_older_stretch() -> None:
    """A session that recovered must not deadlock on past repetition."""
    guard = runtime.loop_guard()
    history = ["grep  x", "grep  x", "grep  x", "pytest  tests"]
    verdict = guard(with_state(call("sys_os_edit", path="src/auth.py"), history))
    assert verdict["result"] == "ALLOW"


def test_loop_guard_abstains_on_non_tool_phases() -> None:
    assert runtime.loop_guard()({"type": "request", "data": {}}) is None
