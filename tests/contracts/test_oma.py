"""OMA contract, through the bridge.

The bridge is the only TypeScript surface in the project. `GET /health` needs no
model and proves the process and the `@open-multi-agent/core` import are intact.
`POST /plan` needs a model, so a provider failure is unreachable rather than a
contract break -- but a 200 with a malformed body is a contract break, since
that is what would reach `linearity.validate` and become a spend decision.
"""

from __future__ import annotations

from urllib.parse import urlparse

from overmind.config import load

from .conftest import http_json, observe, port_open, requires_upstream, unreachable

REQUIRED_NODE_KEYS = {
    "id",
    "role",
    "intent",
    "acceptance",
    "reads",
    "writes",
    "depends_on",
    "exit_check",
}


def bridge_url() -> str:
    """Read the URL from config so the test cannot drift from the real one."""
    url = load().bridge.url.rstrip("/")
    parsed = urlparse(url)
    host, port = parsed.hostname or "127.0.0.1", parsed.port or 80
    if not port_open(host, port):
        unreachable(f"bridge is not listening on {host}:{port} (make dev-up)")
    return url


@requires_upstream
def test_health_reports_the_provider_and_model_it_will_plan_with() -> None:
    status, payload = http_json(f"{bridge_url()}/health", timeout=10)

    assert status == 200, f"bridge /health returned {status}"
    assert payload.get("ok") is True
    assert payload.get("provider"), "/health no longer reports a provider"
    observe("bridge planner", f"{payload.get('provider')}/{payload.get('model')}")


@requires_upstream
def test_plan_returns_a_body_the_python_side_can_validate() -> None:
    status, payload = http_json(
        f"{bridge_url()}/plan",
        {
            "goal": "add a docstring to one existing function",
            "contract": "Reply with one task. Write no code.",
            "max_parallel_hint": 1,
        },
        timeout=180,
    )

    if status >= 500:
        # No credential, provider outage, or coordinator produced nothing
        # parseable. None of these say the interface changed.
        unreachable(f"bridge /plan returned {status}: {str(payload.get('error'))[:200]}")

    assert status == 200, f"bridge /plan returned {status}: {payload}"

    nodes = payload.get("nodes")
    assert isinstance(nodes, list) and nodes, "/plan returned no nodes"

    ambiguity = payload.get("ambiguity")
    assert isinstance(ambiguity, int | float), "/plan no longer reports ambiguity"
    assert 0.0 <= float(ambiguity) <= 1.0

    for node in nodes:
        missing = sorted(REQUIRED_NODE_KEYS - set(node))
        assert not missing, f"node {node.get('id')!r} is missing {missing}"
        assert isinstance(node["exit_check"], dict)
        assert node["exit_check"].get("kind"), "exit_check has no kind"


@requires_upstream
def test_an_unknown_route_is_a_clean_404_not_a_crash() -> None:
    status, _ = http_json(f"{bridge_url()}/nope", timeout=10)
    assert status == 404
