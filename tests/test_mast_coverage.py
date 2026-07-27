"""Does each MAST failure mode actually get blocked?

`docs/MAST-GATES.md` maps 14 failure modes plus two beyond-MAST failures to
gates. Until now that mapping was prose. A table saying `step repetition` is
handled by `loop_detect_semantic` is a claim about a document; this file turns it
into a claim about behaviour.

Every trace is run through the real gate. Nothing here reimplements a gate --
some of these functions were rewritten in T06 precisely because their original
versions passed the traces they should have blocked, and a harness that carries
its own copy of the logic under test would have agreed with the bug.

A mode needs traces in both directions. The blocking case proves the gate has
teeth; the clearing case proves the teeth are aimed. `context_carry` blocked a
perfectly ordered chain A -> B -> C until T06, which no amount of
failure-only testing would have surfaced.

Everything runs offline: `MemoryConfig(enabled=False)` forces the n-gram
similarity path, so the semantic gates are deterministic with no Ruflo, no
network, and no model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from overmind import gates, integrate, introspect, semantic
from overmind.config import MemoryConfig
from overmind.models import GateResult, GateStatus, Plan, Receipt, TaskNode

REPO = Path(__file__).resolve().parent.parent
TRACES = Path(__file__).resolve().parent / "mast"
MAST_DOC = REPO / "docs" / "MAST-GATES.md"

# Forces the offline n-gram measure. Ruflo is never started by this suite.
OFFLINE = MemoryConfig(enabled=False, command=["true"], recall_limit=12)

BLOCKING = {GateStatus.FAIL, GateStatus.HALT}


# -- loading ---------------------------------------------------------------


def load_traces() -> list[dict[str, Any]]:
    traces: list[dict[str, Any]] = []
    for path in sorted(TRACES.glob("*.jsonl")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            trace = json.loads(line)
            trace["_origin"] = f"{path.name}:{lineno}"
            traces.append(trace)
    return traces


TRACE_LIST = load_traces()


def trace_id(trace: dict[str, Any]) -> str:
    return f"{trace['gate']}-{trace['expect']}-{trace['_origin'].split(':')[1]}"


def documented_pairs() -> set[tuple[str, str]]:
    """(mode, gate) pairs claimed by the tables in docs/MAST-GATES.md.

    Parsed rather than duplicated here, so the docs cannot drift from the tests
    in either direction.
    """
    pairs: set[tuple[str, str]] = set()
    in_table = False

    for raw in MAST_DOC.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            in_table = False
            continue

        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2:
            continue

        if cells[1].lower() == "gate":  # header row of a mapping table
            in_table = True
            continue
        if set(cells[0]) <= {"-", ":"}:  # separator row
            continue
        if not in_table:
            continue

        mode = cells[0].lower()
        for gate in cells[1].replace("`", "").split(","):
            if gate.strip():
                pairs.add((mode, gate.strip()))

    return pairs


# -- building objects from trace payloads ---------------------------------


def node_of(raw: dict[str, Any]) -> TaskNode:
    return TaskNode.model_validate(raw)


def receipt_of(raw: dict[str, Any]) -> Receipt:
    return Receipt.model_validate(raw)


def plan_of(raw: dict[str, Any]) -> Plan:
    return Plan.model_validate(raw)


def audit_of(raw: dict[str, Any]) -> introspect.WriteAudit:
    return introspect.WriteAudit(
        node_id=raw["node_id"],
        declared=list(raw["declared"]),
        actual=set(raw["actual"]),
    )


def report_of(raw: dict[str, Any]) -> integrate.IntegrationReport:
    conflict = raw.get("conflict")
    return integrate.IntegrationReport(
        base_branch=raw["base_branch"],
        base_sha=raw["base_sha"],
        merged=list(raw.get("merged", [])),
        empty=list(raw.get("empty", [])),
        conflict=(
            integrate.Conflict(
                node_id=conflict["node_id"],
                branch=conflict["branch"],
                paths=list(conflict["paths"]),
            )
            if conflict
            else None
        ),
        rolled_back=bool(raw.get("rolled_back", False)),
    )


# -- drivers ---------------------------------------------------------------
#
# One per gate. Fixtures carry arguments, not calling conventions: gate
# signatures differ, and expressing them in JSON would mean writing a small
# interpreter that nobody can read and that would itself need testing.


def drive_plan(gate: str, payload: dict[str, Any]) -> list[GateResult]:
    """The four plan-time gates all take a Plan and return a list."""
    plan = plan_of(payload["plan"])
    fn = {
        "explicit_exit": gates.explicit_exit,
        "verify_required": gates.verify_required,
        "cross_vendor_verify": gates.cross_vendor_verify,
        "context_carry": gates.context_carry,
    }[gate]
    return fn(plan)


def drive_prove_disjoint(payload: dict[str, Any]) -> list[GateResult]:
    """`prove_disjoint` compares nodes that ran in the same level.

    `Plan.model_validate` does not compute levels -- the rewriter does, and it is
    not in the loop here -- so the traces are read as one concurrent level unless
    they say otherwise. That is what both of these traces mean: two nodes the
    rewriter judged independent from their declared writes.
    """
    plan = plan_of(payload["plan"])
    plan.levels = payload.get("levels") or [[n.id for n in plan.nodes]]
    audits = {nid: audit_of(raw) for nid, raw in payload["audits"].items()}
    return introspect.prove_disjoint(plan, audits)


def run_trace(trace: dict[str, Any]) -> list[GateResult]:
    gate, payload = trace["gate"], trace["payload"]
    driver = trace["driver"]

    if driver == "plan":
        return drive_plan(gate, payload)
    if driver == "prove_disjoint":
        return drive_prove_disjoint(payload)

    single: dict[str, Any] = {
        "spec_conformance": lambda p: gates.spec_conformance(
            node_of(p["node"]), p["verdict"]
        ),
        "acceptance_drift": lambda p: gates.acceptance_drift(
            node_of(p["node"]), p["restated"], OFFLINE
        ),
        "role_scope": lambda p: gates.role_scope(
            node_of(p["node"]), p["allowed_tools"], receipt_of(p["receipt"])
        ),
        "exit_proof": lambda p: gates.exit_proof(node_of(p["node"]), p["exit_code"]),
        "action_trace": lambda p: gates.action_trace(
            node_of(p["node"]), receipt_of(p["receipt"])
        ),
        "decision_surface": lambda p: gates.decision_surface(receipt_of(p["receipt"])),
        "review_ack": lambda p: gates.review_ack(p["comments"], p["responses"]),
        "checkpoint_continuity": lambda p: gates.checkpoint_continuity(
            p["prior"], p["resumed"]
        ),
        "ambiguity_halt": lambda p: gates.ambiguity_halt(p["score"], p["threshold"]),
        "loop_detect_semantic": lambda p: semantic.loop_detect_semantic(
            receipt_of(p["receipt"]), cfg=OFFLINE
        ),
        "declared_scope": lambda p: introspect.declared_scope(audit_of(p["audit"])),
        "clean_merge": lambda p: integrate.clean_merge(report_of(p["report"])),
    }

    if driver not in single:
        raise AssertionError(f"trace {trace['_origin']} names unknown driver {driver!r}")
    return [single[driver](payload)]


# -- the harness -----------------------------------------------------------


def test_there_are_traces_to_run() -> None:
    """Guards against an empty glob quietly making every other test vacuous."""
    assert len(TRACE_LIST) >= 30


@pytest.mark.parametrize("trace", TRACE_LIST, ids=trace_id)
def test_each_trace_gets_the_verdict_the_fixture_expects(trace: dict[str, Any]) -> None:
    results = run_trace(trace)
    assert results, f"{trace['_origin']}: gate returned nothing"

    blocking = [r for r in results if r.status in BLOCKING]
    expect_blocking = trace["expect"] == "blocking"

    detail = " | ".join(f"{r.gate}={r.status}: {r.detail}" for r in results)
    if expect_blocking:
        assert blocking, f"{trace['_origin']}: expected a block ({trace['why']}), got {detail}"
    else:
        assert not blocking, (
            f"{trace['_origin']}: expected to pass ({trace['why']}), was blocked by {detail}"
        )


@pytest.mark.parametrize("trace", TRACE_LIST, ids=trace_id)
def test_the_reacting_gate_is_the_one_the_fixture_names(trace: dict[str, Any]) -> None:
    """Otherwise a trace could be graded by a neighbouring gate and still look green."""
    results = run_trace(trace)
    assert trace["gate"] in {r.gate for r in results}, (
        f"{trace['_origin']}: no result from {trace['gate']}"
    )


@pytest.mark.parametrize(
    "trace", [t for t in TRACE_LIST if t["expect"] == "blocking"], ids=trace_id
)
def test_a_blocked_trace_reports_the_mast_mode(trace: dict[str, Any]) -> None:
    """A failure that does not name its mode cannot be counted as coverage.

    Compared by prefix: the docs label the two beyond-MAST failures `(textual)`
    and `(semantic)`, while `clean_merge` and `prove_disjoint` both report the
    shared cause they are two views of. PASS results are exempt -- several gates
    leave `mast_mode` empty on success, which is reasonable, since there is no
    failure to classify.
    """
    if trace.get("mast_mode_checked") is False:
        pytest.skip("fixture does not pin this gate's mode string")

    blocked = [r for r in run_trace(trace) if r.status in BLOCKING and r.gate == trace["gate"]]
    assert blocked, f"{trace['_origin']}: {trace['gate']} did not block"
    for result in blocked:
        assert result.mast_mode, f"{trace['_origin']}: blocked with no mast_mode"
        assert trace["mode"].startswith(result.mast_mode.lower()), (
            f"{trace['_origin']}: gate reported {result.mast_mode!r}, "
            f"docs say {trace['mode']!r}"
        )


def test_every_mode_has_a_trace_it_blocks_and_a_trace_it_clears() -> None:
    """The second half is the point. A gate that fails everything covers nothing."""
    seen: dict[str, set[str]] = {}
    for trace in TRACE_LIST:
        seen.setdefault(trace["mode"], set()).add(trace["expect"])

    incomplete = {mode: sorted(kinds) for mode, kinds in seen.items() if len(kinds) < 2}
    assert not incomplete, f"modes tested in only one direction: {incomplete}"


def test_the_docs_and_the_traces_cover_the_same_pairs() -> None:
    """Both directions.

    A mode in the mapping table with no trace is an unverified claim. A trace
    for a pair the docs do not list means the docs are out of date. Either way
    someone reading MAST-GATES.md would be misled.
    """
    documented = documented_pairs()
    tested = {(t["mode"], t["gate"]) for t in TRACE_LIST}

    assert documented, "parsed no mapping rows out of docs/MAST-GATES.md"
    assert not documented - tested, f"documented but never exercised: {sorted(documented - tested)}"
    assert not tested - documented, f"exercised but undocumented: {sorted(tested - documented)}"


def test_all_fourteen_mast_modes_plus_the_two_beyond_are_present() -> None:
    """The headline number in MAST-GATES.md, checked rather than asserted in prose."""
    modes = {mode for mode, _gate in documented_pairs()}
    assert len(modes) == 16, sorted(modes)


def test_the_generated_coverage_table_is_current() -> None:
    """Fails when docs/MAST-COVERAGE.md no longer matches the traces.

    Regenerate with `python tools/gen_coverage.py`.
    """
    import sys

    sys.path.insert(0, str(REPO / "tools"))
    import gen_coverage

    stale = gen_coverage.stale_rows(REPO)
    assert not stale, f"docs/MAST-COVERAGE.md is out of date: {stale}"
