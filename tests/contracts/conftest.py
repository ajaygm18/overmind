"""Shared machinery for upstream contract tests.

Two failure kinds live here and must never be confused:

- **Unreachable.** No network, no `npx`, `omnigent` not installed, bridge not
  running. This says nothing about upstream's interface, so it skips and CI
  stays green. Reporting it as a contract break trains everyone to ignore the
  job, which is what `continue-on-error: true` had already achieved.
- **Changed.** Upstream answered and the answer is not what this repo assumes.
  That fails loudly, because a real run would hit it later and more expensively.

Network-touching tests are opt-in via `OVERMIND_CONTRACTS=1`, so a plain
`pytest` never spawns `npx` or waits on a socket. Pure-Python invariants -- the
Ruflo allowlist -- always run.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import urllib.error
import urllib.request
from typing import Any, NoReturn

import pytest

ENABLED = os.environ.get("OVERMIND_CONTRACTS") == "1"

requires_upstream = pytest.mark.skipif(
    not ENABLED,
    reason="set OVERMIND_CONTRACTS=1 to run contract tests against real upstreams",
)


def unreachable(reason: str) -> NoReturn:
    """Upstream could not be contacted. Not a contract violation."""
    pytest.skip(f"upstream unreachable: {reason}")


def run(argv: list[str], timeout: int = 180) -> subprocess.CompletedProcess[str]:
    """Run a CLI, converting 'not installed' and 'no answer' into skips."""
    try:
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError:
        unreachable(f"{argv[0]} is not installed")
    except subprocess.TimeoutExpired:
        unreachable(f"{argv[0]} did not answer within {timeout}s")


def observe(name: str, version: str) -> None:
    """Record the version actually seen.

    A silent upstream bump is the failure mode this whole job exists for, so the
    observed versions go into the CI summary rather than only into a log line
    nobody scrolls to.
    """
    line = f"- **{name}**: `{version.strip() or 'unknown'}`"
    print(line)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def http_json(
    url: str, payload: dict[str, Any] | None = None, timeout: float = 60.0
) -> tuple[int, dict[str, Any]]:
    """GET or POST JSON. Connection errors skip; HTTP errors are returned.

    An HTTP status is an answer, so it is the caller's business. A refused
    connection is not an answer.
    """
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(  # noqa: S310 - fixed loopback URL
        url, data=data, headers={"content-type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except json.JSONDecodeError:
            return exc.code, {}
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        unreachable(f"{url}: {exc}")
