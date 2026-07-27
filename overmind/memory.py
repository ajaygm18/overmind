"""Ruflo as a vector memory service, and nothing else.

ADR-002. An independent audit of Ruflo v3.5.51 found roughly 10 of its 300+ MCP
tools actually execute; the rest record JSON state. The memory tools are among
the real ones, so this module uses those and hard-refuses the rest.

terminal_execute is excluded despite working: Omnigent's sandboxed execution is
strictly better, and having two execution paths would mean one of them is
unsandboxed.
"""

from __future__ import annotations

import json
import subprocess
from contextlib import suppress
from typing import Any

from .config import MemoryConfig

ALLOWED: frozenset[str] = frozenset(
    {
        "memory_store",
        "memory_search",
        "embeddings_generate",
        "session_save",
    }
)


class RufloToolNotAllowed(Exception):
    """Raised when something tries to dispatch work to Ruflo."""


class MemoryUnavailable(Exception):
    pass


class RufloMemory:
    """Minimal MCP stdio client. Speaks just enough JSON-RPC for four tools."""

    def __init__(self, cfg: MemoryConfig) -> None:
        self._cfg = cfg
        self._proc: subprocess.Popen[str] | None = None
        self._seq = 0

    def __enter__(self) -> RufloMemory:
        if self._cfg.enabled:
            self._start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _start(self) -> None:
        try:
            self._proc = subprocess.Popen(  # noqa: S603 - command comes from local config
                self._cfg.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise MemoryUnavailable(f"cannot start ruflo: {self._cfg.command[0]} not found") from exc

        self._rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "overmind", "version": "0.1.0"},
            },
        )

    def close(self) -> None:
        if self._proc is None:
            return
        with suppress(Exception):
            if self._proc.stdin:
                self._proc.stdin.close()
        with suppress(Exception):
            self._proc.wait(timeout=5)
        with suppress(Exception):
            self._proc.kill()
        self._proc = None

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            raise MemoryUnavailable("ruflo mcp process is not running")

        self._seq += 1
        frame = {"jsonrpc": "2.0", "id": self._seq, "method": method, "params": params}
        self._proc.stdin.write(json.dumps(frame) + "\n")
        self._proc.stdin.flush()

        while True:
            line = self._proc.stdout.readline()
            if not line:
                raise MemoryUnavailable("ruflo mcp closed the stream")
            with suppress(json.JSONDecodeError):
                msg = json.loads(line)
                if msg.get("id") == self._seq:
                    if "error" in msg:
                        raise MemoryUnavailable(str(msg["error"]))
                    result: dict[str, Any] = msg.get("result", {})
                    return result

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """The single chokepoint. The allowlist lives in code so it cannot rot."""
        if tool not in ALLOWED:
            raise RufloToolNotAllowed(
                f"{tool!r} is not allowlisted. overmind uses ruflo for vector memory only; "
                f"execution goes through omnigent (ADR-002). allowed: {sorted(ALLOWED)}"
            )
        if not self._cfg.enabled:
            return {}
        return self._rpc("tools/call", {"name": tool, "arguments": arguments})

    # -- the two write points and one read point (see docs/ARCHITECTURE.md) --

    def record_decision(self, run_id: str, node_id: str, decision: str) -> None:
        """Store a decision, not a transcript. Transcripts are what make recall noisy."""
        self.call(
            "memory_store",
            {
                "namespace": "overmind/decisions",
                "key": f"{run_id}/{node_id}",
                "value": decision,
                "metadata": {"run_id": run_id, "node_id": node_id, "kind": "decision"},
            },
        )

    def record_failure(self, run_id: str, node_id: str, mast_mode: str, detail: str) -> None:
        self.call(
            "memory_store",
            {
                "namespace": "overmind/failures",
                "key": f"{run_id}/{node_id}",
                "value": f"[{mast_mode}] {detail}",
                "metadata": {"run_id": run_id, "node_id": node_id, "mast_mode": mast_mode},
            },
        )

    def recall(self, goal: str, limit: int | None = None) -> list[str]:
        """Prior decisions and failures relevant to this goal, for the planner prompt."""
        if not self._cfg.enabled:
            return []
        out: list[str] = []
        for namespace in ("overmind/decisions", "overmind/failures"):
            with suppress(MemoryUnavailable):
                res = self.call(
                    "memory_search",
                    {
                        "namespace": namespace,
                        "query": goal,
                        "limit": limit or self._cfg.recall_limit,
                    },
                )
                out.extend(_flatten_hits(res))
        return out[: (limit or self._cfg.recall_limit)]


def _flatten_hits(result: dict[str, Any]) -> list[str]:
    """Pull text out of an MCP tool result without assuming a stable shape.

    Ruflo ships releases constantly; being liberal here is cheaper than pinning
    to a response schema that moves.
    """
    hits: list[str] = []
    for item in result.get("content", []) or []:
        if isinstance(item, dict) and item.get("type") == "text":
            text = str(item.get("text", "")).strip()
            if text:
                hits.append(text)
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        for row in structured.get("results", []) or []:
            if isinstance(row, dict) and (val := row.get("value")):
                hits.append(str(val))
    return hits
