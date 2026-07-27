"""Client for the OMA coordinator, exposed as a local service by bridge/planner.ts.

Overmind does not plan. It asks OMA to plan, injects prior decisions recalled
from Ruflo memory, then rewrites what comes back (see linearity.py).
"""

from __future__ import annotations

import httpx

from .config import Config
from .models import Plan


class PlannerUnavailable(Exception):
    pass


class PlanRejected(Exception):
    pass


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

Declare reads and writes exhaustively. Parallel-safety is computed from them; an
under-declared write causes two agents to collide.

Do not add verification tasks. They are inserted automatically after every task
that writes, and they run on a different vendor than the author.

Also return an `ambiguity` score in [0,1]: how much of this goal you had to
guess at. Guessing is not penalised; hiding it is.
"""


def _payload(goal: str, cfg: Config, prior: list[str]) -> dict[str, object]:
    return {
        "goal": goal,
        "contract": PLAN_CONTRACT,
        "prior_decisions": prior,
        "max_parallel_hint": cfg.run.max_parallel,
    }


def plan(goal: str, cfg: Config, prior: list[str] | None = None) -> tuple[Plan, float]:
    """Return the raw (un-rewritten) plan and the coordinator's ambiguity score."""
    url = cfg.bridge.url.rstrip("/") + "/plan"
    try:
        resp = httpx.post(url, json=_payload(goal, cfg, prior or []), timeout=cfg.bridge.timeout_s)
    except httpx.HTTPError as exc:
        raise PlannerUnavailable(
            f"cannot reach the OMA bridge at {cfg.bridge.url} ({exc}). start it with `make bridge`."
        ) from exc

    if resp.status_code != 200:
        raise PlanRejected(f"bridge returned {resp.status_code}: {resp.text[:400]}")

    body = resp.json()
    if "nodes" not in body:
        raise PlanRejected(f"bridge response has no nodes: {list(body)}")

    ambiguity = float(body.get("ambiguity", 0.0))
    return Plan.model_validate({"goal": goal, "nodes": body["nodes"]}), ambiguity


def health(cfg: Config) -> bool:
    try:
        return httpx.get(cfg.bridge.url.rstrip("/") + "/health", timeout=5).status_code == 200
    except httpx.HTTPError:
        return False
