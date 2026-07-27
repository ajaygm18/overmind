"""Omnigent contract: the CLI shape and the policy handlers we emit.

Omnigent is alpha with a moving harness matrix. Two things must hold or a run
fails at session start: the flags `executor.execute` passes still exist, and the
builtin policy handlers a generated spec names still resolve.
"""

from __future__ import annotations

import importlib

import pytest

from overmind.policy_export import (
    UPSTREAM_ASK_ON_OS,
    UPSTREAM_COST_BUDGET,
    UPSTREAM_MAX_TOOL_CALLS,
)

from .conftest import observe, requires_upstream, run, unreachable

# Pinned by T02: executor.execute invokes
#   omnigent run <spec> --non-interactive --json
# and nothing else. The old CI job grepped for --harness, --cwd and --policy,
# which that rewrite deleted -- it was guarding a contract we no longer have.
REQUIRED_FLAGS = ("--non-interactive", "--json")

HANDLERS = (UPSTREAM_MAX_TOOL_CALLS, UPSTREAM_ASK_ON_OS, UPSTREAM_COST_BUDGET)


def test_we_only_claim_handlers_documented_in_policies_md() -> None:
    """Offline. These three strings are the only ones verbatim-confirmed.

    Guessing a dotted path for `block_working_dir_changes` or
    `risk_score_policy` would fail at session start, which is why T03 wrote an
    owned `dir_guard` instead.
    """
    assert HANDLERS == (
        "omnigent.policies.builtins.safety.max_tool_calls_per_session",
        "omnigent.policies.builtins.safety.ask_on_os_tools",
        "omnigent.policies.builtins.cost.cost_budget",
    )


@requires_upstream
def test_omnigent_is_installed_and_its_version_is_recorded() -> None:
    result = run(["omnigent", "--version"])
    if result.returncode != 0:
        unreachable(f"omnigent --version exited {result.returncode}")
    observe("omnigent", result.stdout or result.stderr)


@requires_upstream
@pytest.mark.parametrize("flag", REQUIRED_FLAGS)
def test_run_still_documents_the_flags_the_executor_passes(flag: str) -> None:
    result = run(["omnigent", "run", "--help"])
    help_text = result.stdout + result.stderr
    if not help_text.strip():
        unreachable("omnigent run --help produced no output")
    assert flag in help_text, (
        f"omnigent run no longer documents {flag}; executor.execute passes it on "
        "every node and every session would fail"
    )


@requires_upstream
@pytest.mark.parametrize("handler", HANDLERS)
def test_every_builtin_handler_we_emit_still_resolves(handler: str) -> None:
    """Derived from our own constants, so this catches drift in both directions.

    Upstream renaming a builtin fails here. So does a typo in a string we emit
    into a spec -- which would otherwise surface as a session that dies on
    startup with an import error.
    """
    module_path, _, attribute = handler.rpartition(".")
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        if "omnigent" in str(exc) and "policies" not in str(exc):
            unreachable(f"omnigent is not importable: {exc}")
        raise AssertionError(
            f"{module_path} no longer exists; specs naming {handler} will fail at "
            f"session start ({exc})"
        ) from exc

    assert hasattr(module, attribute), (
        f"{module_path} no longer defines {attribute}; every generated spec names it"
    )
