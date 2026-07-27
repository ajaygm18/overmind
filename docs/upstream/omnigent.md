# Study: Omnigent

**Repository:** `omnigent-ai/omnigent` · Python 3.12+ · Apache 2.0 · alpha · ~7.8k stars
**Pinned at:** `09a035ebb3765054f8312c0d5eba8a5e7f818d49`
**Primary sources read:** `docs/AGENT_YAML_SPEC.md`, `docs/POLICIES.md`, `docs/` index, README
**Role in Overmind:** sandboxed execution and in-session policy enforcement

---

## 1. Philosophy

Omnigent's belief is that **the agent is not the interesting part — the
environment around it is.** An agent is a YAML file naming a harness, a model,
some tools, and a set of policies. Swap `harness: claude-sdk` for
`harness: codex` and the same agent runs on a different vendor's coding agent.
The agent definition is deliberately thin so that the substitution is cheap.

What is *not* thin is the enforcement layer. Two design commitments stand out:

**Least privilege is the default, and the defaults are opinionated.**
`gmail_policy` defaults to read-and-draft with `allow_send: false`.
`gcalendar_policy` defaults to read-only. `gdrive_policy` restricts writes to
files the agent created in this session. The spec instructs authors to "prefer
the narrowest filesystem and network access that supports the task."

**Secrets should not enter the sandbox at all.** The `credential_proxy` mechanism
is the sharpest idea in the repo: a mandatory L7 egress proxy attaches the real
credential on the way out, so the sandbox holds only `oa_cred_*` placeholders.
Inside the sandbox a `databricks` CLI call works; a token exfiltration does not,
because there is no token. Notably it "does not widen egress on its own" — every
host must still be listed in `egress_rules`.

**A third commitment shows in the policy verdicts.** Policies return ALLOW, DENY,
or **ASK** — a first-class "pause for a human" outcome, not an afterthought. And
`cost_budget` at its hard limit is explicitly a *downgrade gate*, not a stop: it
denies only while the session is on an expensive model, telling the user to switch
with `/model`, and allows again once they have. That is a more sophisticated
response to budget exhaustion than killing the run.

## 2. Architecture

```
              agent.yaml  (name, prompt, executor, tools, policies, os_env, terminals)
                   │
                   ▼
         ┌─────────────────────┐
         │   Harness adapter   │  claude-sdk · codex · cursor · kiro-native
         │                     │  pi · antigravity · qwen · kimi · copilot
         │                     │  hermes · openai-agents  (+ *-native variants)
         └──────────┬──────────┘
                    │  every tool call and LLM turn passes through
                    ▼
         ┌─────────────────────┐
         │   Policy engine     │  session → agent spec → server-wide
         │  ALLOW / DENY / ASK │  declaration order; DENY short-circuits
         └──────────┬──────────┘
                    ▼
         ┌─────────────────────┐
         │      Sandbox        │  linux_bwrap · darwin_seatbelt · none
         │                     │  write_paths · read_paths · egress_rules
         │                     │  credential_proxy
         └──────────┬──────────┘
                    ▼
         sys_os_read / write / edit / shell · terminals · MCP tools
```

The critical structural fact: **the policy engine sits between the harness and
every side effect.** It is not a wrapper around the run, it is inline with each
tool call, and it can see accumulated session state (`usage.total_cost_usd`,
`session_state`) at the moment of the call.

## 3. Modules

### Agent spec surface (`AGENT_YAML_SPEC.md`)

| Field | Purpose |
| --- | --- |
| `name` | Stable identifier in sessions and logs |
| `prompt` / `instructions` | Agent-owned system instructions; `instructions` wins, and may be a file path (`AGENTS.md`) resolved relative to the YAML |
| `executor` | `harness`, `model`, `auth` (`databricks` + profile, `api_key`, `provider`, `base_url`) |
| `tools` | `mcp` (command/args or url/headers), `function` (dotted `callable`, JSON-schema `parameters`, or `runtime: client`), `agent` (sub-agent), plus `inherit` / `self` |
| `policies` | Guardrails on requests, responses, tool calls, tool results |
| `params` | Typed user parameters |
| `os_env` | Local OS access and sandbox configuration |
| `terminals` | Named interactive shells |
| `async` / `cancellable` / `timers` | Defaults `true` / `true` / `false` |

Sub-agents are full agents: each picks its own `executor.harness` and `model`,
with `os_env: inherit`, `pass_history`, and `max_sessions`. The spec's own example
is a `cursor` coder with a `claude-sdk` reviewer — **cross-vendor review is
upstream's idea, and Overmind's contribution is making it non-optional.**

Omnigent "may append framework-owned lifecycle or metadata instructions at
runtime after agent and per-request instructions." The prompt is therefore not
fully under the caller's control, which matters when reasoning about determinism.

### Policy engine (`POLICIES.md`)

Three configuration levels, evaluated **session → agent spec → server-wide**, so
an end user can add gates but the ordering means a session policy can DENY before
an admin policy is consulted.

The interface is small enough to be a stable target:

```python
def my_policy(event: PolicyEvent) -> PolicyResponse | None:
    if event["type"] != "tool_call":
        return None                      # abstain
    ...
    return {"result": "DENY", "reason": "..."}
```

`PolicyEvent` carries `type`, `target`, `data` (`name`, `arguments`), `context`
(`actor.run_as`, `usage.total_cost_usd`, token counts), `session_state`, and
`request_data`. `PolicyResponse` carries `result`, optional `reason`, and
`state_updates` supporting `set` / `increment` / `delete` / `append`. Returning
`None` abstains. A factory form takes `factory_params` at build time.

Builtins, grouped: **safety** (`max_tool_calls_per_session`, `ask_on_os_tools`,
`block_skills`, `enforce_sandbox`, `deny_pii_in_llm_request`), **cost**
(`cost_budget`, `user_daily_cost_budget` — per-user per-UTC-day with approvals
remembered across sessions), **integrations** (`github_policy` with
`write_repos` / `write_branches` and shell-command parsing, `gdrive_policy`,
`gmail_policy`, `gcalendar_policy`), **working directory**
(`block_working_dir_changes`), **risk** (`risk_score_policy`, accruing points per
tool and per data-classification label, escalating guarded tools past a
threshold), and **routing** (`deny_trivial_to_expensive_model`, which classifies
the message with a server LLM).

Discoverability is via a `POLICY_REGISTRY` export plus `policy_modules` in server
config; admins can also manage policies at runtime over `POST/GET/PATCH/DELETE
/v1/policies`, with `GET /v1/policy-registry` returning parameter schemas.

## 4. Flow

1. `omnigent run agent.yaml -p "…"` loads and validates the spec.
2. Instructions resolve (`instructions` over `prompt`; file paths relative to the
   YAML). Omnigent may append framework-owned instructions.
3. The harness adapter starts the vendor's coding agent, with auth resolved from
   `executor.auth` or `omnigent setup` config.
4. The sandbox is constructed — platform default if `sandbox.type` is omitted
   (`linux_bwrap` on Linux, `darwin_seatbelt` on macOS).
5. Each LLM turn and tool call is evaluated by the policy chain in order; DENY
   short-circuits, ASK suspends for approval.
6. Allowed calls execute inside the sandbox; egress passes the L7 proxy, which
   swaps credential placeholders for real secrets.
7. Usage and cost accumulate in `context.usage`, visible to later policy
   evaluations — which is what makes `cost_budget` possible as a policy rather
   than a special case.

## 5. What Overmind takes, and what it must not take

**Takes:** the entire execution substrate. One node = one `omnigent run` against
a generated agent YAML, in a git worktree, in a sandbox, under a budget. Overmind
writes no sandbox, no harness adapter, no approval UI.

**Takes:** the harness matrix as the mechanism for cross-vendor review. Overmind
maps its `Role` to a harness and vendor, and the router enforces
`author_vendor != reviewer_vendor`. Omnigent supplies the substitutability;
Overmind supplies the compulsion.

**Takes (correction to Overmind's original design):** the policy engine as the
*right home for the per-tool-call gates*. Overmind's first cut ran every gate
post-hoc against receipts. For gates that answer "did the agent do something it
should not have," post-hoc is defensible. For gates that answer "is the agent
currently stuck in a loop" or "is the agent writing outside its declared scope,"
post-hoc is **strictly worse than in-session**: the budget is already spent and
the damage is already on disk. `semantic.loop_detect_semantic` and
`introspect.declared_scope` should be emitted as Omnigent policies that DENY at
the moment of the offending call, and retained as post-hoc receipt gates only as
defence in depth for harnesses that under-report tool calls. This is recorded as
a task, not as done.

**Must not take:** `terminal_execute`-style unsandboxed escapes, or
`sandbox.type: none` outside local development.

**Must not take:** Omnigent's web UI (`localhost:6767`), nor its sub-agent
mechanism as an orchestration layer. Omnigent sub-agents nest inside one session
and one worktree; Overmind needs sibling nodes in *separate* worktrees that are
later merged. Nesting cannot express that.

### ⚠ Direct conflict to resolve

`block_working_dir_changes` defaults to `block_worktree: true` and
`block_cd: true`, blocking `git worktree add/move/remove` and parsing chained and
wrapped commands to prevent bypasses. **Overmind's executor is built on
`git worktree add`.** These are incompatible by default.

The resolution is not to disable the policy — it exists for a good reason, since
an agent that can change directories or add worktrees can escape its declared
scope. The resolution is that **Overmind creates the worktree from outside the
sandbox, before the session starts, and the agent inside the sandbox never runs a
git worktree command.** Overmind should therefore *enable*
`block_working_dir_changes` in its generated agent YAML with `allowed_dirs` set to
the node's worktree path. The agent is confined to a worktree it did not create
and cannot leave. This conflict, correctly resolved, hardens the design rather
than weakening it — and it is only visible by reading the policy list, which is
the argument for these studies existing.
