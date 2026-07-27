"""The append-only run ledger.

ADR-005: receipts are the source of truth, not logs. The MAST authors annotated
traces averaging 15,000 lines each; prose does not scale as a debugging surface.
A run is structured data, so post-hoc gates, cost accounting, replay, and CI
eval gates all read from the same place.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from .config import ReceiptConfig
from .models import GateResult, Receipt

_REDACTED = "[redacted]"


class Ledger:
    def __init__(self, cfg: ReceiptConfig, run_id: str) -> None:
        self._cfg = cfg
        self.run_id = run_id
        self.path = Path(cfg.dir) / f"{run_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _scrub(self, receipt: Receipt) -> Receipt:
        """Tool arguments can carry secrets. Redaction is opt-in per ADR-005."""
        if not self._cfg.redact_tool_args:
            return receipt
        copy = receipt.model_copy(deep=True)
        for call in copy.tool_calls:
            if "args" in call:
                call["args"] = _REDACTED
        return copy

    def append(self, receipt: Receipt) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(self._scrub(receipt).model_dump_json() + "\n")

    def append_gates(self, plan_hash: str, node_id: str, gates: list[GateResult]) -> None:
        blocking = [g for g in gates if g.blocking]
        self.append(
            Receipt(
                run_id=self.run_id,
                plan_hash=plan_hash,
                node_id=node_id,
                kind="gate",
                gates=gates,
                status="failed" if blocking else "ok",
            )
        )

    def read(self) -> list[Receipt]:
        return list(iter_receipts(self.path))

    def spent(self) -> float:
        return round(sum(r.cost_usd for r in self.read()), 4)

    def decisions(self) -> list[str]:
        return [d for r in self.read() for d in r.decisions]


def iter_receipts(path: Path) -> Iterator[Receipt]:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield Receipt.model_validate(json.loads(line))


def find(cfg: ReceiptConfig, run_id: str) -> Path:
    path = Path(cfg.dir) / f"{run_id}.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"no receipts for run {run_id} at {path}")
    return path


def list_runs(cfg: ReceiptConfig) -> list[str]:
    directory = Path(cfg.dir)
    if not directory.exists():
        return []
    return sorted(p.stem for p in directory.glob("*.jsonl"))


def summarize(receipts: list[Receipt]) -> dict[str, object]:
    """What a run cost and where it broke. Feeds `overmind replay` and CI gates."""
    nodes = [r for r in receipts if r.kind == "node"]
    failed_gates = [g for r in receipts for g in r.gates if g.blocking]
    by_vendor: dict[str, float] = {}
    for r in nodes:
        if r.vendor:
            by_vendor[r.vendor] = round(by_vendor.get(r.vendor, 0.0) + r.cost_usd, 4)
    return {
        "nodes": len(nodes),
        "failed_nodes": [r.node_id for r in nodes if r.status == "failed"],
        "cost_usd": round(sum(r.cost_usd for r in nodes), 4),
        "cost_by_vendor": by_vendor,
        "tokens": sum(r.tokens_in + r.tokens_out for r in nodes),
        "failed_gates": [
            {"gate": g.gate, "mast_mode": g.mast_mode, "detail": g.detail} for g in failed_gates
        ],
        "plan_hashes": sorted({r.plan_hash for r in receipts if r.plan_hash}),
    }
