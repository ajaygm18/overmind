"""MAST-derived gates.

Source: Cemri et al., 'Why Do Multi-Agent LLM Systems Fail?', NeurIPS 2025
(arXiv:2503.13657). 14 failure modes, 200 traces, 7 frameworks, kappa=0.88.
79% of failures are specification or coordination problems rather than model
capability, which is why these are gates over artifacts and not prompt text.

A gate inspects a diff, an exit code, or a receipt. It passes or it does not.
Unlike a prompt instruction, it does not degrade when the model changes.

See docs/MAST-GATES.md for the full mode-to-gate mapping, including the three
gates that are detective rather than preventive.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable

from .models import GateResult, GateStatus, Plan, Receipt, Role, TaskNode

_REJECTION = re.compile(r"\b(reject|won'?t fix|out of scope|disagree|not applicable)\b", re.I)


def _ok(gate: str, mode: str, detail: str = "") -> GateResult:
    return GateResult(gate=gate, status=GateStatus.PASS, mast_mode=mode, detail=detail)


def _fail(gate: str, mode: str, detail: str) -> GateResult:
    return GateResult(gate=gate, status=GateStatus.FAIL, mast_mode=mode, detail=detail)


def _halt(gate: str, mode: str, detail: str) -> GateResult:
    return GateResult(gate=gate, status=GateStatus.HALT, mast_mode=mode, detail=detail)


# --------------------------------------------------------------------------
# Plan-time gates. These run before any money is spent.
# --------------------------------------------------------------------------


def explicit_exit(plan: Plan) -> list[GateResult]:
    """MAST: unaware of termination conditions.

    linearity.validate() already raises on this, so reaching a failure here
    means the plan was mutated after validation. Kept as defence in depth.
    """
    results = []
    for node in plan.nodes:
        if node.exit_check.is_machine_checkable:
            results.append(_ok("explicit_exit", "unaware of termination conditions", node.id))
        else:
            results.append(
                _fail(
                    "explicit_exit",
                    "unaware of termination conditions",
                    f"{node.id}: exit kind {node.exit_check.kind} is not machine-checkable",
                )
            )
    return results


def verify_required(plan: Plan) -> list[GateResult]:
    """MAST: no or incomplete verification.

    Structural. The rewriter inserts verify nodes; this confirms it did, and is
    the reason verification cannot be switched off in config.
    """
    results = []
    for node in plan.nodes:
        if not node.writes or node.role is Role.VERIFIER:
            continue
        verified = any(n.inspects == node.id and n.role is Role.VERIFIER for n in plan.nodes)
        results.append(
            _ok("verify_required", "no or incomplete verification", node.id)
            if verified
            else _fail(
                "verify_required",
                "no or incomplete verification",
                f"{node.id} writes {node.writes} with no verify node",
            )
        )
    return results


def cross_vendor_verify(plan: Plan) -> list[GateResult]:
    """MAST: incorrect verification. Enforced again here; router.audit is the primary."""
    results = []
    for node in plan.nodes:
        if not node.inspects:
            continue
        target = plan.node(node.inspects)
        if node.vendor and node.vendor == target.vendor:
            results.append(
                _fail(
                    "cross_vendor_verify",
                    "incorrect verification",
                    f"{node.id} and {target.id} share vendor {node.vendor}",
                )
            )
        else:
            results.append(
                _ok("cross_vendor_verify", "incorrect verification", f"{node.id}/{target.id}")
            )
    return results


def ambiguity_halt(score: float, threshold: float) -> GateResult:
    """MAST: fail to ask for clarification.

    Halts before spending rather than failing after. The score is model-produced,
    so it inherits the scorer's calibration error — documented, not hidden.
    """
    if score >= threshold:
        return _halt(
            "ambiguity_halt",
            "fail to ask for clarification",
            f"goal ambiguity {score:.2f} >= {threshold:.2f}; clarify before spending",
        )
    return _ok("ambiguity_halt", "fail to ask for clarification", f"ambiguity {score:.2f}")


def context_carry(plan: Plan) -> list[GateResult]:
    """MAST: loss of conversation history.

    When the rewriter serialized two conflicting nodes, the successor must
    inherit the predecessor's transcript. Verified structurally: a node that
    writes files another node also touches must depend on it.
    """
    results = []
    for node in plan.nodes:
        for other in plan.nodes:
            if other.id == node.id or not node.conflicts_with(other):
                continue
            linked = other.id in node.depends_on or node.id in other.depends_on
            if not linked:
                results.append(
                    _fail(
                        "context_carry",
                        "loss of conversation history",
                        f"{node.id} and {other.id} conflict but neither depends on the other",
                    )
                )
    return results or [_ok("context_carry", "loss of conversation history", "all conflicts chained")]


def plan_gates(plan: Plan, ambiguity: float, threshold: float) -> list[GateResult]:
    return [
        *explicit_exit(plan),
        *verify_required(plan),
        *cross_vendor_verify(plan),
        *context_carry(plan),
        ambiguity_halt(ambiguity, threshold),
    ]


# --------------------------------------------------------------------------
# Node-time gates. These run as each node closes.
# --------------------------------------------------------------------------


def role_scope(node: TaskNode, allowed_tools: Iterable[str], receipt: Receipt) -> GateResult:
    """MAST: disobey role specification. Terminate, not warn."""
    allowed = set(allowed_tools)
    used = {str(c.get("tool", "")) for c in receipt.tool_calls}
    outside = sorted(t for t in used - allowed if t)
    if outside:
        return _fail(
            "role_scope",
            "disobey role specification",
            f"{node.id} called tools outside its declared set: {outside}",
        )
    return _ok("role_scope", "disobey role specification", node.id)


def loop_detect(receipt: Receipt, limit: int = 3) -> GateResult:
    """MAST: step repetition. Catches identical repetition only; see docs."""
    signatures = Counter(
        (str(c.get("tool")), repr(sorted((c.get("args") or {}).items())))
        if isinstance(c.get("args"), dict)
        else (str(c.get("tool")), repr(c.get("args")))
        for c in receipt.tool_calls
    )
    for (tool, _args), count in signatures.items():
        if count >= limit:
            return _fail(
                "loop_detect",
                "step repetition",
                f"{receipt.node_id} repeated {tool} with identical args {count} times",
            )
    return _ok("loop_detect", "step repetition", receipt.node_id)


def exit_proof(node: TaskNode, exit_code: int | None) -> GateResult:
    """MAST: premature termination. A node closes on its check, never on assertion."""
    if exit_code is None:
        return _fail(
            "exit_proof",
            "premature termination",
            f"{node.id} produced no result for exit check {node.exit_check.kind}",
        )
    if exit_code != 0:
        return _fail(
            "exit_proof",
            "premature termination",
            f"{node.id} exit check {node.exit_check.kind} returned {exit_code}",
        )
    return _ok("exit_proof", "premature termination", f"{node.id} {node.exit_check.kind} ok")


def spec_conformance(node: TaskNode, verdict: str) -> GateResult:
    """MAST: disobey task specification. Verdict comes from the verify node."""
    if verdict.strip().lower().startswith("pass"):
        return _ok("spec_conformance", "disobey task specification", node.id)
    return _fail(
        "spec_conformance",
        "disobey task specification",
        f"{node.id} did not satisfy: {node.acceptance[:160]}",
    )


def acceptance_drift(node: TaskNode, restated: str) -> GateResult:
    """MAST: task derailment.

    The verifier compares against the ORIGINAL acceptance text. If it restated
    the criterion, we check the restatement still overlaps the original; a
    verifier grading its own paraphrase is how derailment goes unnoticed.
    """
    original = set(re.findall(r"[a-z0-9]{4,}", node.acceptance.lower()))
    echoed = set(re.findall(r"[a-z0-9]{4,}", restated.lower()))
    if not original:
        return _fail("acceptance_drift", "task derailment", f"{node.id} has empty acceptance")
    overlap = len(original & echoed) / len(original)
    if overlap < 0.4:
        return _fail(
            "acceptance_drift",
            "task derailment",
            f"{node.id} verified against a restatement sharing only {overlap:.0%} of the original",
        )
    return _ok("acceptance_drift", "task derailment", f"{node.id} overlap {overlap:.0%}")


def decision_surface(receipt: Receipt) -> GateResult:
    """MAST: information withholding. Detective: a write with no declared decision."""
    if receipt.diff_stat and not receipt.decisions:
        return _fail(
            "decision_surface",
            "information withholding",
            f"{receipt.node_id} changed code but declared no decisions",
        )
    return _ok("decision_surface", "information withholding", receipt.node_id)


def review_ack(comments: list[str], responses: list[str]) -> GateResult:
    """MAST: ignored other agent's input. Silence on a review comment fails."""
    unanswered = len(comments) - len(responses)
    if unanswered > 0:
        return _fail(
            "review_ack",
            "ignored other agent's input",
            f"{unanswered} review comment(s) neither applied nor explicitly rejected",
        )
    silent = [r for r in responses if not r.strip()]
    if silent:
        return _fail("review_ack", "ignored other agent's input", "empty response to a comment")
    rejected_without_reason = [
        r for r in responses if _REJECTION.search(r) and len(r.split()) < 6
    ]
    if rejected_without_reason:
        return _fail(
            "review_ack",
            "ignored other agent's input",
            "a review comment was rejected without a stated reason",
        )
    return _ok("review_ack", "ignored other agent's input", f"{len(comments)} comments answered")


def action_trace(node: TaskNode, receipt: Receipt) -> GateResult:
    """MAST: reasoning-action mismatch. Detective.

    Flags writes to paths the node never declared. Under-declaration is the
    known hole in the linearity gate (ADR-003); this is where it surfaces.
    """
    declared = set(node.writes)
    touched = {
        str(c.get("path"))
        for c in receipt.tool_calls
        if c.get("path") and str(c.get("tool", "")).startswith(("write", "edit", "apply"))
    }
    undeclared = sorted(p for p in touched - declared if p and p != "None")
    if undeclared:
        return _fail(
            "action_trace",
            "reasoning-action mismatch",
            f"{node.id} wrote undeclared paths {undeclared}; parallel safety was computed "
            "from the declared set",
        )
    return _ok("action_trace", "reasoning-action mismatch", node.id)


def checkpoint_continuity(prior: list[str], resumed: list[str]) -> GateResult:
    """MAST: conversation reset. A resume that drops prior decisions is a hard failure."""
    lost = sorted(set(prior) - set(resumed))
    if lost:
        return _fail(
            "checkpoint_continuity",
            "conversation reset",
            f"resume lost {len(lost)} prior decision(s): {lost[:3]}",
        )
    return _ok("checkpoint_continuity", "conversation reset", f"{len(prior)} decisions carried")
