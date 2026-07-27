"""Client for the OMA coordinator, exposed as a local service by bridge/planner.ts.

Overmind does not plan. It asks OMA to plan, injects prior decisions recalled
from Ruflo memory, then rewrites what comes back (see linearity.py).

The bridge validates plans and answers 422 with a list of offending fields
(bridge/schema.ts). This module keeps that detail rather than flattening it into
a truncated string: 'the coordinator omitted tasks[2].exit_check.command' is
actionable, 'bridge returned 422' is not.
"""

from __future__ import annotations

from typing import Any

import httpx

from .config import Config
from .models import Plan


class PlannerUnavailable(Exception):
    """The bridge could not be reached, or did not answer."""


class PlanRejected(Exception):
    """The bridge answered, and the answer is not a usable plan."""


class PlanInvalid(PlanRejected):
    """The coordinator produced a plan that failed validation at the boundary.

    Subclasses PlanRejected so callers that only care about 'no plan' keep
    working, while callers that want the field paths can ask for them.
    """

    def __init__(self, issues: list[dict[str, str]], attempts: int) -> None:
        self.issues = issues
        self.attempts = attempts
        super().__init__(self._message())

    def _message(self) -> str:
        if not self.issues:
            return (
                f"the coordinator failed validation after {self.attempts} attempt(s) "
                "and the bridge reported no field detail"
            )
        lines = [
            f"  {issue.get('field', '?')}: {issue.get('message', 'invalid')}"
            for issue in self.issues
        ]
        return (
            f"the coordinator produced an invalid plan after {self.attempts} attempt(s). "
            f"{len(self.issues)} problem(s):\n" + "\n".join(lines)
        )

    @property
    def fields(self) -> list[str]:
        return [str(issue.get("field", "?")) for issue in self.issues]


PLAN_CONTRACT = """\
Return a task DAG. For every task you MUST provide:
  id           stable slug, lowercase, hyphenated
  role         one of: researcher, implementer, verifier, reviewer
  intent       what this task does
  acceptance   a criterion someone else can check without asking you
  reads        every file path the task will read
  writes       every file path the task will modify or create
  depends_on   ids this task needs completed first
  exit_check   {kind: tests_pass|build_succeeds|schema_valid|command_exit_zero|diff_nonempty,
                command: required only for command_exit_zero}

Every field above is required. Nothing is filled in for you: a task missing one
is rejected and sent back to you, naming the field.

Declare reads and writes exhaustively. Parallel-safety is computed from them; an
under-declared write causes two agents to collide.

Do not add verification tasks. They are inserted automatically after every task
that writes, and they run on a different vendor than the author.

Also return an `ambiguity` score in [0,1]: how much of this goal you had to
guess at. Guessing is not penalised; hiding it is.
"""


def payload(goal: str, cfg: Config, prior: list[str] | None = None) -> dict[str, Any]:
    """Exactly what gets POSTed. Public so `plan --dry-run` can show it."""
    return {
        "goal": goal,
        "contract": PLAN_CONTRACT,
        "prior_decisions": prior or [],
        "max_parallel_hint": cfg.run.max_parallel,
    }


def _body(resp: httpx.Response) -> dict[str, Any]:
    try:
        parsed = resp.json()
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def plan(goal: str, cfg: Config, prior: list[str] | None = None) -> tuple[Plan, float]:
    """Return the raw (un-rewritten) plan and the coordinator's ambiguity score."""
    url = cfg.bridge.url.rstrip("/") + "/plan"
    try:
        resp = httpx.post(url, json=payload(goal, cfg, prior), timeout=cfg.bridge.timeout_s)
    except httpx.HTTPError as exc:
        raise PlannerUnavailable(
            f"cannot reach the OMA bridge at {cfg.bridge.url} ({exc}). start it with `make bridge`."
        ) from exc

    # 422 is the bridge saying the plan is unacceptable, which is a different
    # event from the bridge or provider failing, and gets a different exception.
    if resp.status_code == 422:
        body = _body(resp)
        raw_issues = body.get("issues")
        issues = [i for i in raw_issues if isinstance(i, dict)] if isinstance(raw_issues, list) else []
        raise PlanInvalid(issues, int(body.get("attempts", 1) or 1))

    if resp.status_code != 200:
        raise PlanRejected(f"bridge returned {resp.status_code}: {resp.text[:400]}")

    body = _body(resp)
    nodes = body.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise PlanRejected(f"bridge returned 200 with no usable nodes: {sorted(body)}")
    if not all(isinstance(node, dict) for node in nodes):
        raise PlanRejected("bridge returned 200 with nodes that are not objects")

    ambiguity = body.get("ambiguity", 0.0)
    try:
        score = float(ambiguity)
    except (TypeError, ValueError):
        raise PlanRejected(f"bridge returned a non-numeric ambiguity: {ambiguity!r}") from None

    return Plan.model_validate({"goal": goal, "nodes": nodes}), max(0.0, min(1.0, score))


def health(cfg: Config) -> bool:
    try:
        return httpx.get(cfg.bridge.url.rstrip("/") + "/health", timeout=5).status_code == 200
    except httpx.HTTPError:
        return False
