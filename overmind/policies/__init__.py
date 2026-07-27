"""Overmind gates, compiled to run inside the Omnigent session.

The handlers in `runtime` are imported by Omnigent, not by Overmind. They are
declared in generated agent YAML by dotted path, so this package is part of the
public surface: renaming a handler breaks every previously generated spec.
"""

from .runtime import (
    POLICY_REGISTRY,
    dir_guard,
    loop_guard,
    role_guard,
    scope_guard,
)

__all__ = [
    "POLICY_REGISTRY",
    "dir_guard",
    "loop_guard",
    "role_guard",
    "scope_guard",
]
