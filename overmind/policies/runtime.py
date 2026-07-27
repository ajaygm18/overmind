"""Policy handlers that run inside the Omnigent session.

Overmind's first design ran all fourteen gates post-hoc against receipts. For
the gates that ask "is the record complete?" that is the only possible time.
For the gates that ask "is this write allowed?" or "is this agent stuck right
now?" it is strictly worse than asking during the session: by the time a
receipt exists, the budget is spent and the out-of-scope file is on disk.

Omnigent's policy engine is the correct home for those. It evaluates every tool
call, returns ALLOW / DENY / ASK, sees accumulated `session_state`, and
short-circuits the remaining chain on DENY.

Three constraints shape everything here.

**These functions run in the hot path.** Every tool call pays their cost, so
they do no I/O and open no subprocesses. `find_loop` is called with `cfg=None`,
which pins it to the offline n-gram measure. The stronger embedding measure
stays in the post-hoc gate where a few hundred milliseconds is free.

**They must abstain correctly.** Omnigent evaluates policies for request,
response, tool-call and tool-result phases. A handler that judges a phase it
was not written for is a false positive with a confident-sounding reason.
Returning `None` abstains, and every handler here returns `None` first.

**They are the public surface.** Omnigent imports these by dotted path from
generated YAML, so signatures and names are a compatibility contract.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from ..introspect import covered_by
from ..semantic import describe, find_loop

PolicyEvent = dict[str, Any]
PolicyResponse = dict[str, Any]
Evaluator = Callable[[PolicyEvent], PolicyResponse | None]

ALLOW: PolicyResponse = {"result": "ALLOW"}

WRITE_TOOLS = frozenset({"sys_os_write", "sys_os_edit"})
SHELL_TOOLS = frozenset({"sys_os_shell"})

# Harnesses disagree on the argument name for a path, and a scope guard that
# only understands one of them is a scope guard that can be bypassed by
# switching harness. Checked in order; the first present key wins.
PATH_KEYS: tuple[str, ...] = ("path", "file_path", "file", "target_file", "filename")

# Shell fragments that can write without naming a path in an argument. These do
# not DENY -- a shell command's real target cannot be known without executing
# it, and denying every pipeline would make the sandbox useless. They ASK,
# which is the verdict Omnigent provides for exactly this case.
WRITE_ISH: tuple[str, ...] = (
    ">", ">>", "|", "tee ", "rm ", "mv ", "cp ", "touch ", "mkdir ",
    "sed -i", "truncate", "chmod", "chown", "dd ", "patch ", "install ",
)

# Directory and worktree escapes. Overmind's executor creates the worktree from
# the host process before the session starts, so an agent inside the sandbox has
# no legitimate reason to run any of these.
ESCAPES: tuple[str, ...] = (
    "cd ", "chdir ", "pushd", "popd", "git -c ", "git --git-dir",
    "git worktree", "git clone", "git submodule",
)


def _deny(reason: str) -> PolicyResponse:
    return {"result": "DENY", "reason": reason}


def _ask(reason: str) -> PolicyResponse:
    return {"result": "ASK", "reason": reason}


def _is_tool_call(event: PolicyEvent) -> bool:
    return event.get("type") == "tool_call"


def _tool_name(event: PolicyEvent) -> str:
    data = event.get("data")
    if isinstance(data, dict):
        name = data.get("name")
        if isinstance(name, str) and name:
            return name
    target = event.get("target")
    return target if isinstance(target, str) else ""


def _arguments(event: PolicyEvent) -> dict[str, Any]:
    data = event.get("data")
    if isinstance(data, dict):
        args = data.get("arguments")
        if isinstance(args, dict):
            return args
    return {}


def _path_of(args: dict[str, Any]) -> str:
    for key in PATH_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _command_of(args: dict[str, Any]) -> str:
    for key in ("command", "cmd", "script"):
        value = args.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return " ".join(str(part) for part in value)
    return ""


def _session_list(event: PolicyEvent, key: str) -> list[str]:
    state = event.get("session_state")
    if not isinstance(state, dict):
        return []
    prior = state.get(key)
    if not isinstance(prior, list):
        return []
    return [str(item) for item in prior]


def scope_guard(
    write_paths: Sequence[str] | None = None,
    ask_on_shell: bool = True,
) -> Evaluator:
    """DENY writes outside the node's declared file set.

    This is `introspect.declared_scope` moved to the moment of the write. It
    uses the same `covered_by` matcher, so a path the post-hoc gate would accept
    is accepted here and there is exactly one definition of "in scope".

    An empty declared set means the node declared no writes at all -- a
    reviewer, researcher or verifier -- and every write is denied.

    Shell commands are the honest hard case. Their real target is not knowable
    without running them, so a command that looks like it writes returns ASK
    rather than a guess in either direction. Set `ask_on_shell=False` for
    unattended runs, where the post-hoc gate catches it from git instead.
    """
    declared = [str(p) for p in (write_paths or []) if str(p).strip()]

    def evaluate(event: PolicyEvent) -> PolicyResponse | None:
        if not _is_tool_call(event):
            return None

        tool = _tool_name(event)
        args = _arguments(event)

        if tool in WRITE_TOOLS:
            path = _path_of(args)
            if not path:
                return _ask(f"{tool} named no path; cannot prove it is in scope")
            if not declared:
                return _deny(
                    f"this task declared no writes, so {path!r} is out of scope"
                )
            if not covered_by(declared, path):
                return _deny(
                    f"{path!r} is outside the declared scope {sorted(declared)}. "
                    "parallel safety for this level was computed without it."
                )
            return ALLOW

        if tool in SHELL_TOOLS and ask_on_shell:
            command = _command_of(args).lower()
            if any(fragment in command for fragment in WRITE_ISH):
                return _ask(
                    "shell command may write outside the declared scope "
                    f"{sorted(declared)}"
                )
            return ALLOW

        return None

    return evaluate


def dir_guard(allowed_dirs: Sequence[str] | None = None) -> Evaluator:
    """DENY directory and worktree escapes from the node's worktree.

    Omnigent ships `block_working_dir_changes`, which does this and does it
    well. Its handler import path is not documented, and guessing an import
    path produces a policy that fails at session start rather than a policy that
    works -- so this is implemented here. If upstream documents the path, this
    becomes a thin alias and the tests carry over unchanged.

    Overmind's executor runs `git worktree add` from the host process before the
    session starts, so denying it inside the sandbox costs the agent nothing it
    legitimately needs.
    """
    allowed = [str(d) for d in (allowed_dirs or []) if str(d).strip()]

    def evaluate(event: PolicyEvent) -> PolicyResponse | None:
        if not _is_tool_call(event):
            return None
        if _tool_name(event) not in SHELL_TOOLS:
            return None

        command = _command_of(_arguments(event))
        lowered = command.lower()

        for fragment in ESCAPES:
            if fragment not in lowered:
                continue
            # A `cd` into the node's own worktree is harmless; anything else is
            # the agent leaving the box it was given.
            if fragment in ("cd ", "chdir ") and any(d in command for d in allowed):
                continue
            return _deny(
                f"{fragment.strip()!r} would leave this task's worktree. "
                f"allowed: {allowed or ['<none>']}"
            )
        return ALLOW

    return evaluate


def role_guard(role: str = "", may_write: bool = True) -> Evaluator:
    """DENY writes from roles whose job is to inspect, not to produce.

    `scope_guard` already denies these, since an inspector declares no writes.
    This exists so the *reason* names the failure correctly. "a reviewer wrote
    code" is a role-boundary violation, and reporting it as a scope violation
    would lose the distinction that makes it worth catching.
    """
    label = role or "this role"

    def evaluate(event: PolicyEvent) -> PolicyResponse | None:
        if may_write or not _is_tool_call(event):
            return None
        if _tool_name(event) not in WRITE_TOOLS:
            return None
        return _deny(
            f"{label} inspects and reports; producing edits is another role's "
            "job. write it up instead of writing it."
        )

    return evaluate


def loop_guard(
    threshold: float = 0.92,
    window: int = 3,
    history: int = 12,
    state_key: str = "overmind_recent_calls",
) -> Evaluator:
    """DENY the call that completes a semantic loop.

    `semantic.find_loop` is the single implementation of loop detection, shared
    with the post-hoc gate, so both paths agree on what a loop is. The offline
    n-gram measure is used deliberately: this runs on every tool call, and
    spawning a Ruflo subprocess to embed three strings per call would cost more
    than the loop it prevents.

    History is bounded and rewritten with `set` rather than grown with `append`,
    because an unbounded list in session state is a slow leak in a long session.
    """

    def evaluate(event: PolicyEvent) -> PolicyResponse | None:
        if not _is_tool_call(event):
            return None

        tool = _tool_name(event)
        if not tool:
            return None

        args = _arguments(event)
        current = describe(
            {"tool": tool, "path": _path_of(args), "args": args}
        )
        if not current:
            return None

        recent = [*_session_list(event, state_key), current][-history:]
        finding, source = find_loop(
            recent, threshold=threshold, window=window, cfg=None
        )

        remember: PolicyResponse = {
            "state_updates": [
                {"key": state_key, "action": "set", "value": recent}
            ]
        }

        # Only a loop ending on *this* call is this call's fault. An older
        # stretch of repetition has already been denied once; denying it again
        # would deadlock a session that has since recovered.
        if finding is not None and finding.start + finding.length == len(recent):
            return {
                **remember,
                "result": "DENY",
                "reason": (
                    f"this is the {finding.length}rd near-identical action in a row "
                    f"(similarity {finding.similarity}, via {source}): "
                    f"{finding.sample!r}. change approach or report being stuck."
                ),
            }

        return {**remember, **ALLOW}

    return evaluate


POLICY_REGISTRY: list[dict[str, Any]] = [
    {
        "handler": "overmind.policies.runtime.scope_guard",
        "kind": "factory",
        "name": "Overmind declared scope",
        "description": "Deny writes outside the task's declared file set.",
        "params_schema": {
            "type": "object",
            "properties": {
                "write_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Declared writable paths, globs or directories",
                },
                "ask_on_shell": {
                    "type": "boolean",
                    "description": "Ask for approval on shell commands that may write",
                },
            },
            "required": ["write_paths"],
        },
    },
    {
        "handler": "overmind.policies.runtime.dir_guard",
        "kind": "factory",
        "name": "Overmind worktree confinement",
        "description": "Deny cd, pushd and git worktree escapes from the task worktree.",
        "params_schema": {
            "type": "object",
            "properties": {
                "allowed_dirs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Directories the agent may occupy",
                }
            },
            "required": ["allowed_dirs"],
        },
    },
    {
        "handler": "overmind.policies.runtime.role_guard",
        "kind": "factory",
        "name": "Overmind role boundary",
        "description": "Deny edits from inspection-only roles.",
        "params_schema": {
            "type": "object",
            "properties": {
                "role": {"type": "string"},
                "may_write": {"type": "boolean"},
            },
            "required": ["role", "may_write"],
        },
    },
    {
        "handler": "overmind.policies.runtime.loop_guard",
        "kind": "factory",
        "name": "Overmind semantic loop guard",
        "description": "Deny the call completing a run of near-identical actions.",
        "params_schema": {
            "type": "object",
            "properties": {
                "threshold": {"type": "number"},
                "window": {"type": "integer"},
                "history": {"type": "integer"},
                "state_key": {"type": "string"},
            },
        },
    },
]
