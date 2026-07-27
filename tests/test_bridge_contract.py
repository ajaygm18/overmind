"""The boundary between the coordinator and the Python side.

Everything here runs offline. `httpx.post` is stubbed, because what is under
test is how this repo reacts to each answer the bridge can give -- not whether a
model is reachable. tests/contracts/test_oma.py covers the live path.

The last group asserts on the TypeScript as text. That is unusual and
deliberate: the coercions T08 removed were silent by construction, and the only
job that runs the real bridge needs npm plus a provider key, so a reintroduced
`|| 'implementer'` would otherwise reach main unnoticed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from overmind import planner
from overmind.config import load
from overmind.models import Plan

REPO = Path(__file__).resolve().parent.parent
BRIDGE = REPO / "bridge"


@pytest.fixture
def cfg() -> Any:
    return load(REPO / "overmind.toml")


class StubResponse:
    """Only the three members planner.plan touches."""

    def __init__(self, status: int, body: Any, text: str | None = None) -> None:
        self.status_code = status
        self._body = body
        self.text = text if text is not None else str(body)

    def json(self) -> Any:
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


def answer(monkeypatch: pytest.MonkeyPatch, response: StubResponse) -> dict[str, Any]:
    """Stub the POST and capture what was sent."""
    sent: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> StubResponse:
        sent["url"] = url
        sent["json"] = kwargs.get("json")
        sent["timeout"] = kwargs.get("timeout")
        return response

    monkeypatch.setattr(planner.httpx, "post", fake_post)
    return sent


VALID_NODE = {
    "id": "add-docstring",
    "role": "implementer",
    "intent": "document one function",
    "acceptance": "the function has a docstring naming its return type",
    "reads": ["overmind/semantic.py"],
    "writes": ["overmind/semantic.py"],
    "depends_on": [],
    "exit_check": {"kind": "tests_pass"},
}


# -- what gets sent --------------------------------------------------------


def test_the_payload_carries_the_contract_the_recall_and_the_parallel_hint(
    monkeypatch: pytest.MonkeyPatch, cfg: Any
) -> None:
    sent = answer(monkeypatch, StubResponse(200, {"nodes": [VALID_NODE], "ambiguity": 0.1}))

    planner.plan("a goal", cfg, ["we chose sqlite in run 3"])

    assert sent["json"]["goal"] == "a goal"
    assert "exit_check" in sent["json"]["contract"]
    assert sent["json"]["prior_decisions"] == ["we chose sqlite in run 3"]
    assert sent["json"]["max_parallel_hint"] == cfg.run.max_parallel
    assert sent["timeout"] == cfg.bridge.timeout_s


def test_payload_is_public_so_dry_run_shows_the_real_thing(cfg: Any) -> None:
    """If --dry-run built its own dict it would drift from what is POSTed."""
    assert planner.payload("g", cfg, ["d"])["contract"] == planner.PLAN_CONTRACT


def test_the_contract_states_that_nothing_is_defaulted(cfg: Any) -> None:
    """The bridge stopped filling fields in; the prompt has to say so."""
    assert "Every field above is required" in planner.PLAN_CONTRACT


# -- 422: the plan is unacceptable ----------------------------------------


def test_a_422_names_every_offending_field(monkeypatch: pytest.MonkeyPatch, cfg: Any) -> None:
    answer(
        monkeypatch,
        StubResponse(
            422,
            {
                "error": "plan failed validation after 2 attempt(s)",
                "attempts": 2,
                "issues": [
                    {"field": "tasks[0].exit_check.command", "message": "command_exit_zero requires the command to run"},
                    {"field": "tasks[1].acceptance", "message": "missing acceptance"},
                ],
            },
        ),
    )

    with pytest.raises(planner.PlanInvalid) as caught:
        planner.plan("a goal", cfg)

    assert caught.value.fields == ["tasks[0].exit_check.command", "tasks[1].acceptance"]
    assert caught.value.attempts == 2
    message = str(caught.value)
    assert "tasks[0].exit_check.command" in message
    assert "tasks[1].acceptance" in message
    assert "2 problem(s)" in message


def test_plan_invalid_is_catchable_as_plan_rejected(
    monkeypatch: pytest.MonkeyPatch, cfg: Any
) -> None:
    answer(monkeypatch, StubResponse(422, {"issues": [], "attempts": 2}))
    with pytest.raises(planner.PlanRejected):
        planner.plan("a goal", cfg)


def test_a_422_with_no_detail_still_says_what_happened(
    monkeypatch: pytest.MonkeyPatch, cfg: Any
) -> None:
    answer(monkeypatch, StubResponse(422, {"error": "nope"}))
    with pytest.raises(planner.PlanInvalid, match="no field detail"):
        planner.plan("a goal", cfg)


def test_a_422_with_a_garbled_body_does_not_raise_a_json_error(
    monkeypatch: pytest.MonkeyPatch, cfg: Any
) -> None:
    answer(monkeypatch, StubResponse(422, "<html>proxy</html>"))
    with pytest.raises(planner.PlanInvalid):
        planner.plan("a goal", cfg)


# -- everything else the bridge can answer -------------------------------


def test_a_500_is_not_reported_as_an_invalid_plan(
    monkeypatch: pytest.MonkeyPatch, cfg: Any
) -> None:
    """A provider outage is not the coordinator's fault and must read differently."""
    answer(monkeypatch, StubResponse(500, {"error": "provider timeout"}))

    with pytest.raises(planner.PlanRejected) as caught:
        planner.plan("a goal", cfg)

    assert not isinstance(caught.value, planner.PlanInvalid)
    assert "500" in str(caught.value)


def test_a_connection_failure_is_unavailable_not_rejected(
    monkeypatch: pytest.MonkeyPatch, cfg: Any
) -> None:
    def boom(url: str, **kwargs: Any) -> StubResponse:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(planner.httpx, "post", boom)

    with pytest.raises(planner.PlannerUnavailable, match="make bridge"):
        planner.plan("a goal", cfg)


@pytest.mark.parametrize(
    "body",
    [
        {"ambiguity": 0.2},
        {"nodes": []},
        {"nodes": "add-docstring"},
        {"nodes": ["add-docstring"]},
    ],
)
def test_a_200_without_usable_nodes_is_rejected(
    monkeypatch: pytest.MonkeyPatch, cfg: Any, body: dict[str, Any]
) -> None:
    """The old client accepted any 200 carrying a 'nodes' key of any shape."""
    answer(monkeypatch, StubResponse(200, body))
    with pytest.raises(planner.PlanRejected):
        planner.plan("a goal", cfg)


def test_a_non_numeric_ambiguity_is_rejected_not_coerced_to_zero(
    monkeypatch: pytest.MonkeyPatch, cfg: Any
) -> None:
    """Zero would silently disable ambiguity_halt, which is the gate that stops
    a run before it spends anything on a goal nobody pinned down."""
    answer(monkeypatch, StubResponse(200, {"nodes": [VALID_NODE], "ambiguity": "high"}))
    with pytest.raises(planner.PlanRejected, match="non-numeric ambiguity"):
        planner.plan("a goal", cfg)


def test_ambiguity_is_clamped_to_the_unit_interval(
    monkeypatch: pytest.MonkeyPatch, cfg: Any
) -> None:
    answer(monkeypatch, StubResponse(200, {"nodes": [VALID_NODE], "ambiguity": 4.2}))
    _, ambiguity = planner.plan("a goal", cfg)
    assert ambiguity == 1.0


def test_a_valid_answer_becomes_a_plan(monkeypatch: pytest.MonkeyPatch, cfg: Any) -> None:
    answer(monkeypatch, StubResponse(200, {"nodes": [VALID_NODE], "ambiguity": 0.35}))

    built, ambiguity = planner.plan("document one function", cfg)

    assert isinstance(built, Plan)
    assert [n.id for n in built.nodes] == ["add-docstring"]
    assert built.node("add-docstring").writes == ["overmind/semantic.py"]
    assert ambiguity == pytest.approx(0.35)


# -- the coercions this task removed must stay removed --------------------


def read(name: str) -> str:
    path = BRIDGE / name
    if not path.exists():  # pragma: no cover - only on a broken checkout
        pytest.skip(f"{path} not present")
    return path.read_text(encoding="utf-8")


def test_the_bridge_validates_instead_of_normalising() -> None:
    source = read("planner.ts")
    assert "validatePlan" in source
    assert "function normalise" not in source, (
        "normalise() repaired unrecognised exit kinds and missing roles; "
        "schema.ts rejects them instead"
    )


def test_no_default_role_is_invented() -> None:
    """Defaulting to implementer can hand write access to a research task."""
    assert "'implementer'" not in read("planner.ts")


def test_an_unknown_exit_kind_is_not_rewritten_to_tests_pass() -> None:
    schema = read("schema.ts")
    assert "unknown exit kind" in schema
    assert "command_exit_zero requires the command" in schema


def test_exactly_one_retry() -> None:
    """Zero wastes a recoverable omission; more than one burns tokens to reach
    the same 422."""
    assert "MAX_ATTEMPTS = 2" in read("planner.ts")


def test_the_retry_feeds_the_validation_errors_back() -> None:
    source = read("planner.ts")
    assert "correctionBlock" in source
    assert "describeIssues" in source


def test_the_validator_agrees_with_the_python_exit_kinds() -> None:
    """model_assertion is deliberately absent: linearity.validate rejects
    non-machine-checkable exits, so accepting one here defers the failure."""
    schema = read("schema.ts")
    for kind in ("tests_pass", "build_succeeds", "schema_valid", "command_exit_zero", "diff_nonempty"):
        assert f"'{kind}'" in schema
    assert "model_assertion" not in schema
