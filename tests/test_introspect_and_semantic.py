"""Tests for the measured-write and semantic-repetition layers.

These two modules exist because the first cut of this repo trusted the planner
about file scope and compared tool calls by string equality. Both weaknesses
are asserted here as regressions, so they cannot come back quietly.

Everything runs offline: no git repository, no model, no upstream process.
"""

from __future__ import annotations

import pytest

from overmind import gates, introspect, semantic
from overmind.config import MemoryConfig
from overmind.models import ExitCheck, ExitKind, Plan, Receipt, Role, TaskNode


def node(node_id: str, *, writes: list[str] | None = None, reads: list[str] | None = None) -> TaskNode:
    return TaskNode(
        id=node_id,
        role=Role.IMPLEMENTER,
        intent=f"do {node_id}",
        acceptance=f"{node_id} is done",
        reads=reads or [],
        writes=writes or [],
        exit_check=ExitCheck(kind=ExitKind.TESTS_PASS),
    )


# -- path coverage ----------------------------------------------------------


def test_exact_glob_and_directory_declarations_all_count_as_covered() -> None:
    """Planners legitimately declare all three ways; flagging any of them would
    make the gate fire on honest nodes and be switched off."""
    assert introspect.covered_by(["src/auth.py"], "src/auth.py")
    assert introspect.covered_by(["src/*.py"], "src/auth.py")
    assert introspect.covered_by(["src/"], "src/auth/device.py")
    assert introspect.covered_by(["src"], "src/auth/device.py")
    assert introspect.covered_by(["./src/auth.py"], "src/auth.py")
    assert not introspect.covered_by(["src/auth.py"], "src/session.py")
    assert not introspect.covered_by(["src/"], "tests/test_auth.py")


# -- write audits -----------------------------------------------------------


def test_undeclared_write_is_detected() -> None:
    audit = introspect.WriteAudit(
        node_id="impl",
        declared=["src/auth/device.py"],
        actual={"src/auth/device.py", "src/auth/session.py"},
    )
    assert audit.undeclared == {"src/auth/session.py"}
    result = introspect.declared_scope(audit)
    assert result.blocking
    assert result.mast_mode == "disobey task specification"


def test_over_declaration_is_reported_but_not_a_failure() -> None:
    """Over-declaring costs parallelism, not correctness."""
    audit = introspect.WriteAudit(
        node_id="impl",
        declared=["src/a.py", "src/b.py"],
        actual={"src/a.py"},
    )
    assert audit.unused == {"src/b.py"}
    assert not introspect.declared_scope(audit).blocking


def test_lesson_is_only_produced_when_there_is_something_to_learn() -> None:
    clean = introspect.WriteAudit("impl", ["src/a.py"], {"src/a.py"})
    dirty = introspect.WriteAudit("impl", ["src/a.py"], {"src/a.py", "src/b.py"})
    assert introspect.lesson(clean) is None
    assert "src/b.py" in (introspect.lesson(dirty) or "")


# -- retroactive disjointness proof ----------------------------------------


def test_concurrent_nodes_that_actually_collided_are_caught() -> None:
    """The rewriter allowed these to run together because their DECLARED sets
    were disjoint. Their measured sets were not."""
    p = Plan(
        goal="g",
        nodes=[node("a", writes=["src/a.py"]), node("b", writes=["src/b.py"])],
        levels=[["a", "b"]],
    )
    audits = {
        "a": introspect.WriteAudit("a", ["src/a.py"], {"src/a.py", "src/shared.py"}),
        "b": introspect.WriteAudit("b", ["src/b.py"], {"src/b.py", "src/shared.py"}),
    }
    failures = [g for g in introspect.prove_disjoint(p, audits) if g.blocking]
    assert failures
    assert failures[0].mast_mode == "conflicting implicit decisions"
    assert "src/shared.py" in failures[0].detail


def test_serialized_nodes_sharing_a_file_are_not_flagged() -> None:
    """A node that depends on another legitimately builds on its output."""
    second = node("b", writes=["src/shared.py"])
    second.depends_on = ["a"]
    p = Plan(
        goal="g",
        nodes=[node("a", writes=["src/shared.py"]), second],
        levels=[["a", "b"]],
    )
    audits = {
        "a": introspect.WriteAudit("a", ["src/shared.py"], {"src/shared.py"}),
        "b": introspect.WriteAudit("b", ["src/shared.py"], {"src/shared.py"}),
    }
    assert not [g for g in introspect.prove_disjoint(p, audits) if g.blocking]


def test_disjoint_concurrent_nodes_pass() -> None:
    p = Plan(
        goal="g",
        nodes=[node("a", writes=["src/a.py"]), node("b", writes=["src/b.py"])],
        levels=[["a", "b"]],
    )
    audits = {
        "a": introspect.WriteAudit("a", ["src/a.py"], {"src/a.py"}),
        "b": introspect.WriteAudit("b", ["src/b.py"], {"src/b.py"}),
    }
    assert not [g for g in introspect.prove_disjoint(p, audits) if g.blocking]


# -- semantic repetition ----------------------------------------------------


def call(tool: str, **args: object) -> dict[str, object]:
    return {"tool": tool, "args": args}


def test_argument_order_does_not_make_two_calls_look_different() -> None:
    left = semantic.describe({"tool": "grep", "args": {"q": "device", "path": "src"}})
    right = semantic.describe({"tool": "grep", "args": {"path": "src", "q": "device"}})
    assert left == right


def test_rephrased_repetition_is_caught_where_exact_matching_failed() -> None:
    """The exact motivating case: a stuck agent rewording the same search.

    String equality sees three distinct productive actions here.
    """
    receipt = Receipt(
        run_id="r",
        plan_hash="h",
        node_id="impl",
        tool_calls=[
            call("grep", q="device_code handling"),
            call("grep", q="device code handling"),
            call("grep", q="device-code handling"),
        ],
    )
    result = semantic.loop_detect_semantic(receipt, threshold=0.7)
    assert result.blocking
    assert result.mast_mode == "step repetition"


def test_identical_calls_are_still_caught() -> None:
    """The new gate must subsume the old one it replaced."""
    receipt = Receipt(
        run_id="r",
        plan_hash="h",
        node_id="impl",
        tool_calls=[call("grep", q="device") for _ in range(3)],
    )
    assert semantic.loop_detect_semantic(receipt).blocking


def test_genuine_progress_is_not_flagged() -> None:
    receipt = Receipt(
        run_id="r",
        plan_hash="h",
        node_id="impl",
        tool_calls=[
            call("read", path="src/auth/device.py"),
            call("write", path="src/auth/device.py"),
            call("bash", cmd="pytest tests/test_device.py"),
        ],
    )
    assert not semantic.loop_detect_semantic(receipt).blocking


def test_repetition_must_be_consecutive_to_count() -> None:
    """Revisiting a file later is normal work, not a loop."""
    receipt = Receipt(
        run_id="r",
        plan_hash="h",
        node_id="impl",
        tool_calls=[
            call("grep", q="device"),
            call("write", path="src/auth/device.py"),
            call("grep", q="device"),
            call("bash", cmd="pytest"),
        ],
    )
    assert not semantic.loop_detect_semantic(receipt).blocking


def test_short_traces_cannot_loop() -> None:
    receipt = Receipt(run_id="r", plan_hash="h", node_id="impl", tool_calls=[call("grep", q="x")])
    assert not semantic.loop_detect_semantic(receipt).blocking


def test_fallback_is_used_and_named_when_memory_is_off() -> None:
    """CI has no Ruflo, so the reported source must say so rather than imply
    embeddings were used."""
    _, source = semantic.find_loop(["a b c d", "a b c d", "a b c d"], cfg=None)
    assert source == "ngram"


def test_ngram_similarity_bounds() -> None:
    assert semantic.ngram_similarity("same text here", "same text here") == 1.0
    assert semantic.ngram_similarity("totally unrelated", "xyzzy plugh") < 0.3


def test_cosine_handles_degenerate_vectors() -> None:
    assert semantic.cosine([], []) == 0.0
    assert semantic.cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
    assert semantic.cosine([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0


def test_restatement_fidelity_separates_paraphrase_from_substitution() -> None:
    original = "the device authorization endpoint returns a user_code and verification_uri"
    faithful = "the device authorization endpoint returns user_code and verification_uri"
    unrelated = "the code is well structured and readable"
    assert semantic.restatement_fidelity(original, faithful) > semantic.restatement_fidelity(
        original, unrelated
    )


def test_embedding_payload_shapes_are_parsed_or_rejected_cleanly() -> None:
    """Upstream ships constantly; an unrecognised shape must fall back, not raise."""
    assert semantic._parse_vectors([[1.0, 2.0], [3.0, 4.0]], 2) == [[1.0, 2.0], [3.0, 4.0]]
    assert semantic._parse_vectors({"embeddings": [[1.0], [2.0]]}, 2) == [[1.0], [2.0]]
    assert semantic._parse_vectors([{"embedding": [1.0]}, {"vector": [2.0]}], 2) == [[1.0], [2.0]]
    assert semantic._parse_vectors({"unexpected": "shape"}, 2) is None
    assert semantic._parse_vectors([[1.0]], 2) is None
    assert semantic._parse_vectors("not json", 2) is None


# -- refusing to prove disjointness from nothing -----------------------------


def test_an_unscheduled_multi_node_plan_is_refused_not_passed() -> None:
    """A plan whose levels were never populated used to sail through: the gate
    iterated `levels`, found no concurrent pairs, and reported PASS. That is a
    statement about the plan's metadata, not about the run."""
    p = Plan(goal="g", nodes=[node("a", writes=["src/a.py"]), node("b", writes=["src/b.py"])])
    assert p.levels == []
    audits = {
        "a": introspect.WriteAudit("a", ["src/a.py"], {"src/a.py", "src/shared.py"}),
        "b": introspect.WriteAudit("b", ["src/b.py"], {"src/b.py", "src/shared.py"}),
    }
    failures = [g for g in introspect.prove_disjoint(p, audits) if g.blocking]
    assert failures, "an unscheduled plan cannot prove anything about concurrency"


def test_an_audited_node_missing_from_every_level_is_refused() -> None:
    """Partial levels are worse than none: the gate would report on the nodes it
    knows about and stay silent about the one it does not."""
    p = Plan(
        goal="g",
        nodes=[node("a", writes=["src/a.py"]), node("b", writes=["src/b.py"])],
        levels=[["a"]],
    )
    audits = {
        "a": introspect.WriteAudit("a", ["src/a.py"], {"src/a.py"}),
        "b": introspect.WriteAudit("b", ["src/b.py"], {"src/b.py"}),
    }
    failures = [g for g in introspect.prove_disjoint(p, audits) if g.blocking]
    assert failures
    assert "b" in failures[0].detail


def test_a_single_node_plan_still_passes_trivially() -> None:
    """One node cannot collide with itself, so the refusal must not fire here --
    otherwise every serial run reports a spurious failure and the gate gets
    switched off."""
    p = Plan(goal="g", nodes=[node("a", writes=["src/a.py"])])
    audits = {"a": introspect.WriteAudit("a", ["src/a.py"], {"src/a.py", "src/extra.py"})}
    assert not [g for g in introspect.prove_disjoint(p, audits) if g.blocking]


# -- required embeddings ----------------------------------------------------

REQUIRED = MemoryConfig(
    enabled=True, command=["true"], recall_limit=4, require_embeddings=True
)
OPTIONAL = MemoryConfig(enabled=False, command=["true"], recall_limit=4)


@pytest.fixture
def no_ruflo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the embedding path unreachable without spawning anything.

    CI has no Ruflo either way; failing at construction keeps the test
    hermetic instead of depending on how a dead MCP process happens to fail.
    """

    def unavailable(cfg: object) -> object:
        raise semantic.MemoryUnavailable("no ruflo under test")

    monkeypatch.setattr(semantic, "RufloMemory", unavailable)


def test_requiring_embeddings_from_disabled_memory_is_rejected_at_load() -> None:
    """A contradiction in config must fail at load, not at the first gate an
    hour into a run."""
    with pytest.raises(ValueError, match="require_embeddings"):
        MemoryConfig(enabled=False, require_embeddings=True)


def test_a_clean_trace_measured_by_the_fallback_fails_when_embeddings_were_required(
    no_ruflo: None,
) -> None:
    """The motivating case. 'No loop found by the weaker measure' is not
    evidence of no loop, and this run said it did not accept the weaker
    measure."""
    receipt = Receipt(
        run_id="r",
        plan_hash="h",
        node_id="impl",
        tool_calls=[
            call("read", path="src/auth/device.py"),
            call("write", path="src/auth/device.py"),
            call("bash", cmd="pytest tests/test_device.py"),
        ],
    )
    result = semantic.loop_detect_semantic(receipt, cfg=REQUIRED)
    assert result.blocking
    assert result.mast_mode == "step repetition"
    assert "require_embeddings" in result.detail


def test_the_same_trace_passes_when_the_fallback_was_acceptable(no_ruflo: None) -> None:
    receipt = Receipt(
        run_id="r",
        plan_hash="h",
        node_id="impl",
        tool_calls=[
            call("read", path="src/auth/device.py"),
            call("write", path="src/auth/device.py"),
            call("bash", cmd="pytest tests/test_device.py"),
        ],
    )
    assert not semantic.loop_detect_semantic(receipt, cfg=OPTIONAL).blocking


def test_a_real_loop_is_reported_as_a_loop_not_as_a_degradation(no_ruflo: None) -> None:
    """Order matters: the finding is the useful message. Reporting the missing
    measure instead would hide the loop the weaker measure just caught."""
    receipt = Receipt(
        run_id="r",
        plan_hash="h",
        node_id="impl",
        tool_calls=[call("grep", q="device") for _ in range(3)],
    )
    result = semantic.loop_detect_semantic(receipt, cfg=REQUIRED)
    assert result.blocking
    assert "near-identical" in result.detail
    assert "require_embeddings" not in result.detail


def test_degraded_is_only_true_when_the_stronger_measure_was_asked_for() -> None:
    assert semantic.degraded("ngram", REQUIRED)
    assert not semantic.degraded("ruflo-embeddings", REQUIRED)
    assert not semantic.degraded("ngram", OPTIONAL)
    assert not semantic.degraded("ngram", None)


def test_acceptance_drift_will_not_pass_on_an_unrequested_measure(no_ruflo: None) -> None:
    """The drift gate has the same hole: a high n-gram score looks exactly like
    a high embedding score once it reaches the report."""
    n = node("impl")
    faithful = n.acceptance
    assert not gates.acceptance_drift(n, faithful).blocking
    assert not gates.acceptance_drift(n, faithful, OPTIONAL).blocking

    result = gates.acceptance_drift(n, faithful, REQUIRED)
    assert result.blocking
    assert result.mast_mode == "task derailment"
