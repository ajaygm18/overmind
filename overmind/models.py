"""Plan and receipt shapes.

These are the contract between the OMA bridge, the rewriter, the executor, and
the ledger. Every field exists because some gate needs it; nothing here is
decorative.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Role(StrEnum):
    PLANNER = "planner"
    IMPLEMENTER = "implementer"
    VERIFIER = "verifier"
    REVIEWER = "reviewer"
    RESEARCHER = "researcher"


class ExitKind(StrEnum):
    """How a node proves it is done.

    MODEL_ASSERTION is intentionally present so the plan validator can reject
    it by name. MAST calls this 'unaware of termination conditions'; a node
    that finishes because the model said so has no exit condition at all.
    """

    TESTS_PASS = "tests_pass"
    BUILD_SUCCEEDS = "build_succeeds"
    SCHEMA_VALID = "schema_valid"
    COMMAND_EXIT_ZERO = "command_exit_zero"
    DIFF_NONEMPTY = "diff_nonempty"
    MODEL_ASSERTION = "model_assertion"  # rejected at plan validation


MACHINE_CHECKABLE: frozenset[ExitKind] = frozenset(
    {
        ExitKind.TESTS_PASS,
        ExitKind.BUILD_SUCCEEDS,
        ExitKind.SCHEMA_VALID,
        ExitKind.COMMAND_EXIT_ZERO,
        ExitKind.DIFF_NONEMPTY,
    }
)


class ExitCheck(BaseModel):
    kind: ExitKind
    command: str | None = None

    @property
    def is_machine_checkable(self) -> bool:
        return self.kind in MACHINE_CHECKABLE


class TaskNode(BaseModel):
    """One unit of work. Maps 1:1 to one Omnigent session and one worktree."""

    id: str
    role: Role
    intent: str

    # The acceptance criterion is captured from the ORIGINAL goal decomposition
    # and never rewritten by the worker. acceptance_drift compares against this.
    acceptance: str

    # Declared file sets. The linearity gate is only as honest as these are;
    # see ADR-003 for the caveat.
    reads: list[str] = Field(default_factory=list)
    writes: list[str] = Field(default_factory=list)

    depends_on: list[str] = Field(default_factory=list)
    exit_check: ExitCheck

    # Filled by the router, not the planner.
    vendor: str | None = None
    harness: str | None = None

    # Set when the rewriter inserted this node rather than the planner.
    synthesized: bool = False
    # For verify/review nodes: which node's output is under inspection.
    inspects: str | None = None

    budget_usd: float | None = None

    @field_validator("writes", "reads")
    @classmethod
    def _normalize_paths(cls, v: list[str]) -> list[str]:
        return sorted({p.lstrip("./") for p in v})

    @property
    def write_set(self) -> frozenset[str]:
        return frozenset(self.writes)

    def conflicts_with(self, other: TaskNode) -> bool:
        """True if these two cannot safely run in parallel.

        Write/write is an obvious conflict. Write/read is also a conflict: the
        reader would observe a stale tree and make decisions against it, which
        is the 'conflicting implicit decisions' failure, not a merge conflict.
        """
        if self.write_set & other.write_set:
            return True
        if self.write_set & frozenset(other.reads):
            return True
        return bool(frozenset(self.reads) & other.write_set)


class Plan(BaseModel):
    goal: str
    nodes: list[TaskNode]
    # Populated by linearity.rewrite: lists of node ids safe to run together.
    levels: list[list[str]] = Field(default_factory=list)
    rewritten: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def node(self, node_id: str) -> TaskNode:
        for n in self.nodes:
            if n.id == node_id:
                return n
        raise KeyError(f"no such node: {node_id}")

    def content_hash(self) -> str:
        """Stable hash of the executable shape of the plan.

        Excludes timestamps so a replay of the same plan hashes identically.
        Receipts carry this hash; a replay that produces a different hash has
        diverged and says so instead of quietly running something else.
        """
        payload = {
            "goal": self.goal,
            "levels": self.levels,
            "nodes": [
                {
                    "id": n.id,
                    "role": str(n.role),
                    "intent": n.intent,
                    "acceptance": n.acceptance,
                    "reads": n.reads,
                    "writes": n.writes,
                    "depends_on": sorted(n.depends_on),
                    "exit": str(n.exit_check.kind),
                    "vendor": n.vendor,
                    "inspects": n.inspects,
                }
                for n in sorted(self.nodes, key=lambda x: x.id)
            ],
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


class GateStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    HALT = "halt"  # needs a human; not a failure


class GateResult(BaseModel):
    gate: str
    status: GateStatus
    mast_mode: str | None = None
    detail: str = ""

    @property
    def blocking(self) -> bool:
        return self.status in (GateStatus.FAIL, GateStatus.HALT)


class Receipt(BaseModel):
    """One append-only ledger entry. The unit of post-hoc analysis and replay."""

    run_id: str
    plan_hash: str
    node_id: str
    kind: Literal["node", "gate", "run"] = "node"

    role: Role | None = None
    vendor: str | None = None
    harness: str | None = None

    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0

    tool_calls: list[dict[str, object]] = Field(default_factory=list)
    # Decisions the node made that were not implied by its intent. MAST calls
    # the absence of these 'information withholding'.
    decisions: list[str] = Field(default_factory=list)

    diff_stat: str | None = None
    worktree: str | None = None
    gates: list[GateResult] = Field(default_factory=list)

    status: Literal["ok", "failed", "halted", "skipped"] = "ok"
    error: str | None = None
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
