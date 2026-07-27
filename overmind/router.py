"""Vendor and harness assignment.

One rule that is not negotiable: whoever inspects a node's output runs on a
different vendor than whoever produced it (ADR-004, borrowed from Omnigent's
Polly). A model that made a mistake is disproportionately likely to rate that
mistake acceptable.
"""

from __future__ import annotations

from .config import Config
from .models import Plan, Role, TaskNode


class RoutingError(Exception):
    pass


def _preferred(cfg: Config, role: Role) -> str:
    vendor = cfg.roles.get(str(role))
    if vendor:
        return vendor
    # No preference declared: pick deterministically so plan hashes stay stable.
    return sorted(cfg.vendors)[0]


def _different_from(cfg: Config, vendor: str) -> str:
    others = sorted(v for v in cfg.vendors if v != vendor)
    if not others:
        raise RoutingError(
            "cannot satisfy cross-vendor inspection with a single vendor configured"
        )
    return others[0]


def _assign(cfg: Config, node: TaskNode, vendor: str) -> None:
    node.vendor = vendor
    node.harness = cfg.harness_for(vendor)


def route(plan: Plan, cfg: Config) -> None:
    """Assign vendor + harness to every node, in place.

    Two passes: producers first, then inspectors, because an inspector's vendor
    is a function of its target's vendor.
    """
    inspectors = [n for n in plan.nodes if n.inspects]
    producers = [n for n in plan.nodes if not n.inspects]

    for node in producers:
        _assign(cfg, node, _preferred(cfg, node.role))

    for node in inspectors:
        target = plan.node(node.inspects) if node.inspects else None
        if target is None or target.vendor is None:
            raise RoutingError(f"node {node.id!r} inspects an unrouted node")

        wanted = _preferred(cfg, node.role)
        vendor = wanted if wanted != target.vendor else _different_from(cfg, target.vendor)
        _assign(cfg, node, vendor)

    audit(plan)


def audit(plan: Plan) -> None:
    """Hard error on any same-vendor inspection pair.

    Called at the end of route() and again before execution, because a config
    reload or a resume could in principle produce a stale assignment.
    """
    for node in plan.nodes:
        if not node.inspects:
            continue
        target = plan.node(node.inspects)
        if node.vendor == target.vendor:
            raise RoutingError(
                f"{node.id!r} would inspect {target.id!r} on the same vendor "
                f"({node.vendor}). self-review shares blind spots; refusing."
            )


def distribute_budget(plan: Plan, total_usd: float) -> None:
    """Split the run budget across nodes.

    Inspection is cheaper than production — it reads a diff rather than
    exploring a repo — so inspectors get a third of a producer's share. This is
    a heuristic, and the per-node figure is a ceiling, not a reservation.
    """
    weights = {n.id: (1.0 if not n.inspects else 0.33) for n in plan.nodes}
    denom = sum(weights.values()) or 1.0
    for node in plan.nodes:
        node.budget_usd = round(total_usd * weights[node.id] / denom, 4)
