# Development environment

What runs where, and why two of the four moving parts are deliberately not
containerised.

## The four parts

| Part | Runs as | Why there |
|---|---|---|
| `overmind` CLI | Python 3.12 on the host | Drives git worktrees in your repository |
| OMA bridge | Container (`docker compose`) | One TypeScript service, one dependency, no host state |
| Omnigent | Host binary | Executes the coding agents against real worktrees with your credentials |
| Ruflo memory | stdio subprocess of the CLI | MCP over stdin/stdout; there is no network protocol to containerise |

The last two are the interesting ones.

**Ruflo** is spoken to over stdio (`npx ruflo@latest mcp start`, see
`overmind/memory.py`). Putting it in a container would mean putting a network
boundary across a pipe, and MCP-over-stdio has no network transport here. The
subprocess is started and stopped by `RufloMemory.__enter__`/`__exit__`, so it
has no lifecycle for compose to manage.

**Omnigent** needs the actual worktree on the actual filesystem, plus your
provider credentials from `~/.omnigent/config.yaml`. Containerising it would
require bind-mounting the repository *and* forwarding credentials into the
container — more moving parts and a wider blast radius than installing it. The
worktree confinement that keeps an agent inside its own directory is enforced by
policy and sandbox config (ADR-010), not by a container boundary.

## Quickstart

```bash
cp .env.example .env          # add the provider key that matches the planner
make dev-up                   # bridge + jaeger
make setup                    # python deps, omnigent, ruflo probe
overmind doctor               # says which part is unhappy, and how
```

Then, without spending anything:

```bash
overmind plan "add token refresh to the auth module" --dry-run
```

`--dry-run` prints the exact request the bridge would receive, including the
decisions recalled from memory, and makes no model call. It is the fastest way to
confirm the bridge, the config, and memory recall all agree before a run costs
money.

## Ports

| Port | Service | Notes |
|---|---|---|
| 7801 | OMA bridge | Must match `[bridge] url` in `overmind.toml` |
| 4318 | Jaeger, OTLP/HTTP | Target for `overmind export --otlp` |
| 16686 | Jaeger UI | http://127.0.0.1:16686 |

Every port is published on `127.0.0.1` only. The bridge takes a goal, calls a
paid model with it, and has no authentication of its own; it must not be
reachable off the host.

## Looking at a run

```bash
overmind export <run-id> --otlp http://127.0.0.1:4318
```

Then open the Jaeger UI and filter on service `overmind`. One trace per run, one
span per node, gate results as span events.

Span durations are **derived from ledger order**, not measured: a receipt records
when it was appended and has no start timestamp, so a node's span runs from the
previous ledger entry to its own. Every span says so via
`overmind.timing.source`. Read the shape of the run and the per-node costs; do
not read the millisecond durations as latency.

Tool arguments are never exported. Ledger redaction is opt-in
(`[receipts] redact_tool_args`), so a receipt on disk may hold a secret you chose
not to scrub locally — spans go to a third-party backend, so only tool names and
counts leave the machine.

## Running without Docker

Nothing requires compose. Two terminals:

```bash
make bridge      # terminal 1
overmind doctor  # terminal 2
```

`make bridge` runs `npm start` in `bridge/`, which is the same command the image
runs. There is no separate containerised code path to drift.

## Tests

```bash
make test        # offline: no model, no network, no upstreams
make lint typecheck
```

The default suite excludes `tests/contracts/`, which asserts against the real
upstream CLIs and is owned by its own CI job. To run those locally:

```bash
OVERMIND_CONTRACTS=1 pytest tests/contracts -q -rs
```

`-rs` prints skip reasons. A skip there means an upstream was unreachable; a
failure means an upstream changed. The two are different findings and are
reported differently on purpose.

## Build reproducibility

`bridge/` has no `package-lock.json`, so `npm install` in the image resolves
within the declared semver ranges at build time. Two builds a month apart can
pick different `@open-multi-agent/core` patches. That is a known gap recorded in
`LIMITATIONS.md`; committing a lockfile is the fix.
