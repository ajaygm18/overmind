"""MAST-derived gates.

Source: Cemri et al., 'Why Do Multi-Agent LLM Systems Fail?', NeurIPS 2025
(arXiv:2503.13657). 14 failure modes, 200 traces, 7 frameworks, kappa=0.88.
79% of failures are specification or coordination problems rather than model
capability, which is why these are gates over artifacts and not prompt text.

A gate inspects a diff, an exit code, or a receipt. It passes or it does not.
Unlike a prompt instruction, it does not degrade when the model changes.

Some of these now have in-session counterparts in `policies/runtime.py`, which
can DENY a bad call instead of reporting it afterwards. The overlap is
deliberate: a policy sees what the harness reports, while these gates read git
and exit codes, so a harness that under-reports its tool calls defeats the
former and not the latter. See ADR-009.

See docs/MAST-GATES.md for the full mode-to-gate mapping, including the three
gates that are detective rather than preventive.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable

from .config import MemoryConfig
from .models import GateResult, GateStatus, Plan, Receipt, Role, TaskNode
from .semantic import degraded, restatement_fidelity_measured

_REJECTION = re.compile(r"\b(reject|won'?t fix|out of scope|disagree|not applicable)\b", re.I)
_VERDICT = re.compile(r"^\s*(pass|fail)\b", re.I)
_WORD = re.compile(r"[a-z0-9]{4,}")

# Minimum similarity between the original acceptance text and the verifier's
# restatement of it. Lower than a word-overlap threshold would be, because the
# measure is similarity of meaning: a faithful paraphrase scores well below 1.0
# and must still pass.
DRIFT_THRESHOLD = 0.55

# A verdict shorter than this is a vote, not a finding.
MIN_VERDICT_WORDS = 5

# Roles that inspect. Mirrors policy_export.INSPECTION_ROLES; the two are
# checked against each other in tests rather than sharing a mutable import, so
# that neither module can silently widen the other's definition.
INSPECTION_ROLES = frozenset({Role.VERIFIER, Role.REVIEWER, Role.RESEARCHER, Role.PLANNER})


def _ok(gate: str, mode: str, detail: str = "") -> GateResult:
    return GateResult(gate=gate, status=GateStatus.PASS, mast_mode=mode, detail=detail)


def _fail(gate: str, mode: str, detail: str) -> GateResult:
    return GateResult(gate=gate, status=GateStatus.FAIL, mast_mode=mode, detail=detail)


def _halt(gate: str, mode: str, detail: str) -> GateResult:
    return GateResult(gate=gate, status=GateStatus.HALT, mast_mode=mode, detail=detail)


def _word_overlap(original: str, restated: str) -> float:
    """Second opinion only. Never load-bearing; see `acceptance_drift`."""
    left = set(_WORD.findall(original.lower()))
    right = set(_WORD.findall(restated.lower()))
    return len(left & right) / len(left) if left else 0.0


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

    The threshold itself is the weak part. `overmind.calibration` holds the
    labelled goal corpus and the chooser that would derive it from recorded
    planner scores; until those scores exist the shipped 0.65 is provisional,
    and it says so in overmind.toml, in that module, and in LIMITATIONS.md.
    """
    if score >= threshold:
        return _halt(
            "ambiguity_halt",
            "fail to ask for clarification",
            f"goal ambiguity {score:.2f} >= {threshold:.2f}; clarify before spending",
        )
    return _ok("ambiguity_halt", "fail to ask for clarification", f"ambiguity {score:.2f}")


def _reachable(plan: Plan) -> dict[str, set[str]]:
    """For each node, every node it transitively depends on.

    Needed because ordering, not adjacency, is what makes two conflicting nodes
    safe. Cycles cannot occur -- linearity.validate topologically sorts the plan
    before this runs -- but the walk is iterative anyway so a malformed plan
    produces a gate failure rather than a recursion error.
    """
    direct = {node.id: set(node.depends_on) for node in plan.nodes}
    closure: dict[str, set[str]] = {}

    for node_id in direct:
        seen: set[str] = set()
        stack = list(direct[node_id])
        while stack:
            current = stack.pop()
            if current in seen or current not in direct:
                continue
            seen.add(current)
            stack.extend(direct[current])
        closure[node_id] = seen

    return closure


def context_carry(plan: Plan) -> list[GateResult]:
    """MAST: loss of conversation history.

    When the rewriter serialized two conflicting nodes, the successor must
    inherit the predecessor's transcript. Verified structurally: two nodes that
    write the same file must be *ordered* with respect to each other.

    Ordering means reachable, not adjacent. The earlier version required a
    direct `depends_on` edge, which failed a perfectly safe chain A -> B -> C
    whenever A and C touched the same file -- a false positive that would have
    trained users to ignore this gate.
    """
    closure = _reachable(plan)
    results: list[GateResult] = []
    seen_pairs: set[tuple[str, str]] = set()

    for node in plan.nodes:
        for other in plan.nodes:
            if other.id == node.id or not node.conflicts_with(other):
                continue
            pair = (min(node.id, other.id), max(node.id, other.id))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)

            ordered = other.id in closure[node.id] or node.id in closure[other.id]
            if not ordered:
                results.append(
                    _fail(
                        "context_carry",
                        "loss of conversation history",
                        f"{node.id} and {other.id} write the same path but neither is "
                        "ordered before the other; the second cannot see the first's work",
                    )
                )

    if results:
        return results
    return [
        _ok(
            "context_carry",
            "loss of conversation history",
            f"{len(seen_pairs)} conflicting pair(s), all ordered",
        )
    ]


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
    """MAST: disobey role specification. Terminate, not warn.

    Two questions, not one. Did the node use a tool outside its declared set --
    and, for an inspection role, did it produce a diff at all? The second is the
    one that matters: a verifier that can edit code can make its own check pass,
    and it needs no unusual tool to do it.
    """
    if node.role in INSPECTION_ROLES and receipt.diff_stat:
        return _fail(
            "role_scope",
            "disobey role specification",
            f"{node.id} has role {node.role} and must not produce edits, but changed "
            f"code: {receipt.diff_stat.strip()[:160]}",
        )

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
    """MAST: step repetition. Catches identical repetition only; see docs.

    Superseded in practice by `semantic.loop_detect_semantic` and by the
    in-session `policies.runtime.loop_guard`. Retained because it needs no
    similarity measure at all, so it still works when both are unavailable.
    """
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
    """MAST: disobey task specification. Verdict comes from the verify node.

    An unparseable verdict is not a pass. Neither is the bare word 'pass': the
    verifier was asked whether the acceptance criterion is met, and a one-word
    answer is a vote rather than a finding. Treating either as success is how a
    verification step becomes a formality.

    Deliberately does not re-check exit codes or declared-vs-actual writes.
    `exit_proof` and `action_trace` own those, and a single failure reported by
    three gates reads as three problems.
    """
    text = verdict.strip()
    match = _VERDICT.match(text)

    if not match:
        return _fail(
            "spec_conformance",
            "disobey task specification",
            f"{node.id}: verdict does not begin with PASS or FAIL, so it cannot be "
            f"read as a judgement: {text[:120]!r}",
        )

    if match.group(1).lower() == "fail":
        return _fail(
            "spec_conformance",
            "disobey task specification",
            f"{node.id} did not satisfy: {node.acceptance[:160]}",
        )

    if len(text.split()) < MIN_VERDICT_WORDS:
        return _fail(
            "spec_conformance",
            "disobey task specification",
            f"{node.id}: verdict {text[:60]!r} states a result with no evidence for it",
        )

    return _ok("spec_conformance", "disobey task specification", node.id)


def acceptance_drift(
    node: TaskNode, restated: str, cfg: MemoryConfig | None = None
) -> GateResult:
    """MAST: task derailment.

    The verifier compares against the ORIGINAL acceptance text. If it restated
    the criterion, the restatement must still mean the same thing -- a verifier
    grading its own paraphrase is how derailment goes unnoticed.

    The measure is similarity of meaning, not shared vocabulary. Word overlap
    scored 'the login endpoint returns 200' against 'auth works' as total drift
    while scoring a restatement that reuses the words and checks something else
    as perfect fidelity, which inverts the gate's purpose.

    `detail` reports the fidelity, the measure that produced it, and word
    overlap as a second opinion. Overlap is never load-bearing: the fallback
    from embeddings to n-grams happens inside `restatement_fidelity_measured`.

    That fallback is silent by default and must not be. Under `[memory]
    require_embeddings` a score that came from n-grams cannot clear this gate,
    because an n-gram score above the threshold says the two texts share
    characters, not that they mean the same thing.
    """
    original = node.acceptance.strip()
    if not original:
        return _fail("acceptance_drift", "task derailment", f"{node.id} has empty acceptance")

    if not restated.strip():
        return _fail(
            "acceptance_drift",
            "task derailment",
            f"{node.id}: verifier restated nothing, so there is no evidence it read "
            "the original criterion",
        )

    fidelity, source = restatement_fidelity_measured(original, restated, cfg)
    overlap = _word_overlap(original, restated)

    if fidelity < DRIFT_THRESHOLD:
        return _fail(
            "acceptance_drift",
            "task derailment",
            f"{node.id} was verified against a restatement with fidelity {fidelity:.2f} "
            f"(< {DRIFT_THRESHOLD:.2f}, measured via {source}, word overlap "
            f"{overlap:.0%}): {restated[:120]!r}",
        )

    if degraded(source, cfg):
        return _fail(
            "acceptance_drift",
            "task derailment",
            f"{node.id} scored {fidelity:.2f} via {source} because ruflo embeddings "
            "were unavailable, and this run set [memory] require_embeddings = true. "
            "a passing n-gram score says the two texts share characters, not that "
            "the restatement preserved the criterion's meaning.",
        )

    return _ok(
        "acceptance_drift",
        "task derailment",
        f"{node.id} fidelity {fidelity:.2f} via {source}, word overlap {overlap:.0%}",
    )


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
