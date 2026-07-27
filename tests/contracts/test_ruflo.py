"""Ruflo contract: four memory tools, and nothing that executes.

Ruflo has shipped 1,488 releases in ten months and an independent audit found
roughly 10 of its 300+ MCP tools actually execute. ADR-002 uses it for vector
memory only. The allowlist is the security boundary that makes that true.
"""

from __future__ import annotations

import uuid

import pytest

from overmind.config import MemoryConfig
from overmind.memory import ALLOWED, MemoryUnavailable, RufloMemory, RufloToolNotAllowed

from .conftest import observe, requires_upstream, run, unreachable

OFFLINE = MemoryConfig(enabled=False, command=["true"], recall_limit=12)


# -- invariants: no network, always run ------------------------------------


def test_the_allowlist_is_exactly_the_four_documented_tools() -> None:
    """Widening this set is a decision, so it must break a test."""
    assert ALLOWED == {
        "memory_store",
        "memory_search",
        "embeddings_generate",
        "session_save",
    }


@pytest.mark.parametrize(
    "tool",
    ["agent_spawn", "task_assign", "workflow_execute", "neural_train", "terminal_execute"],
)
def test_execution_tools_are_refused_before_any_process_starts(tool: str) -> None:
    """terminal_execute is included deliberately: it works, and is still refused.

    Two execution paths would mean one of them is unsandboxed (ADR-002).
    """
    with pytest.raises(RufloToolNotAllowed):
        RufloMemory(OFFLINE).call(tool, {})


def test_the_allowlist_is_checked_before_the_enabled_flag() -> None:
    """Otherwise disabling memory would silently permit a dispatch attempt."""
    with pytest.raises(RufloToolNotAllowed):
        RufloMemory(OFFLINE).call("agent_spawn", {"agent": "x"})


# -- against the real upstream --------------------------------------------


@requires_upstream
def test_ruflo_cli_is_reachable_and_its_version_is_recorded() -> None:
    result = run(["npx", "--yes", "ruflo@latest", "--version"])
    if result.returncode != 0:
        unreachable(f"ruflo --version exited {result.returncode}: {result.stderr[:200]}")
    observe("ruflo", result.stdout)


@requires_upstream
def test_the_four_allowlisted_tools_are_still_advertised() -> None:
    """If upstream renames one, memory degrades silently at runtime.

    `_rpc` is private and used deliberately: this asserts on the MCP tool list,
    which no public method exposes, and a contract test is the right place to
    reach through.
    """
    cfg = MemoryConfig(enabled=True, command=["npx", "--yes", "ruflo@latest", "mcp", "start"])
    try:
        with RufloMemory(cfg) as mem:
            listing = mem._rpc("tools/list", {})
    except MemoryUnavailable as exc:
        unreachable(f"ruflo mcp did not start: {exc}")

    names = {
        str(tool.get("name"))
        for tool in listing.get("tools", [])
        if isinstance(tool, dict)
    }
    assert names, "ruflo mcp advertised no tools at all"

    missing = sorted(ALLOWED - names)
    assert not missing, f"ruflo no longer advertises {missing}; memory would fail at runtime"


@requires_upstream
def test_store_then_search_round_trips_a_value() -> None:
    """The audit says these two execute. This is the assertion that they do."""
    marker = f"overmind-contract-{uuid.uuid4().hex[:12]}"
    cfg = MemoryConfig(enabled=True, command=["npx", "--yes", "ruflo@latest", "mcp", "start"])

    try:
        with RufloMemory(cfg) as mem:
            mem.record_decision("contract-run", "n1", f"decision {marker}")
            hits = mem.recall(marker, limit=25)
    except MemoryUnavailable as exc:
        unreachable(f"ruflo mcp unavailable: {exc}")

    assert any(marker in hit for hit in hits), (
        "memory_store accepted a value that memory_search cannot find; "
        "recall is silently returning nothing"
    )
