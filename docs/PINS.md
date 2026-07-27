# Version surface

What this repository depends on, what is actually pinned, and what is not.

ADR-001 says adapters are "pinned and contract-tested to make breakage loud".
Half of that is true today. The contract tests exist and are blocking
(`tests/contracts/`, own CI job). The pinning is weaker than the sentence
implies, and the difference is set out below rather than left to be discovered.

## Overmind itself

| | |
|---|---|
| Version | `0.1.0` (`pyproject.toml`, `overmind version`) |
| Python | `>=3.12` — required, not preferred: `StrEnum` and PEP 695 syntax are used throughout |
| License | Apache-2.0 |

## Python dependencies

All lower bounds, no upper bounds, no lockfile.

| Package | Constraint | Why it is here |
|---|---|---|
| `pydantic` | `>=2.9` | Every plan and receipt shape; `model_validate` is the trust boundary |
| `httpx` | `>=0.27` | Bridge client and the OTLP POST |
| `typer` | `>=0.12` | CLI |
| `rich` | `>=13.7` | CLI output |
| `tomli-w` | `>=1.0` | Writing config |
| `pyyaml` | `>=6.0` | Agent specs are YAML because Omnigent reads YAML |

Dev: `pytest>=8.3`, `pytest-asyncio>=0.24`, `mypy>=1.11`, `ruff>=0.6`,
`types-PyYAML>=6.0`.

**Not pinned.** A `uv.lock` would make CI and a fresh clone install identical
trees. There is no lock file, so they install compatible ones. `mypy --strict`
and `ruff` run in CI, which catches the breakages that matter for a library of
this size, but it is not the same guarantee.

## Bridge (Node)

| Package | Constraint |
|---|---|
| `@open-multi-agent/core` | `^0.9.0` |
| `tsx` | `^4.19.0` |
| `typescript` | `^5.6.0` |
| `@types/node` | `^22.7.0` |
| Node runtime | `>=22`; image is `node:22-alpine` |

**Not pinned.** No `package-lock.json`. `npm install` — in `make upstreams` and in
the Docker build — resolves within those carets at build time, so two images
built a month apart can carry different `@open-multi-agent/core` patches.
Committing a lockfile fixes it and requires an npm run.

## Upstreams installed at runtime

These are the ones ADR-001 is really about, and they float deliberately.

| Upstream | How it is installed | Pinned? |
|---|---|---|
| Omnigent | `uv tool install --python 3.12 omnigent` | No — latest |
| Ruflo | `npx ruflo@latest mcp start` | No — latest, per invocation |
| OMA | `@open-multi-agent/core` via the bridge | Caret range |

Floating is a choice, not an oversight. Omnigent is alpha and its sandbox
hardening is the main reason to compose it; pinning would mean deliberately
running the older sandbox. Ruflo ships releases at a rate no pin would keep up
with (1,488 releases, 55 alphas) and Overmind uses four of its tools behind an
allowlist (ADR-002), so the exposed surface is tiny.

The cost is that an upstream can change under a working checkout. That is what
`tests/contracts/` is for: it asserts the specific surfaces this repo calls, and
it distinguishes *unreachable* (skip) from *changed* (fail), so drift is a red
build rather than a mystery at runtime.

## Development containers

| Image | Tag | Why pinned |
|---|---|---|
| `node` | `22-alpine` | Matches `engines.node`; a node 24 release must not silently change what the bridge runs |
| `jaegertracing/all-in-one` | `1.62.0` | Fully pinned; it only has to ingest OTLP/HTTP on 4318 |

## Upstream revisions the design was read against

The studies in `docs/upstream/` are evidence-based, which is only meaningful with
the revision attached. These are the exact objects that were read.

| Upstream | Object | SHA |
|---|---|---|
| OMA | `README.md` | `bffccee78e714743c496719b1e3afd76861f69a5` |
| OMA | docs tree ref | `86919ae415bb599108b3180ba2227a01a04b7a73` |
| Omnigent | `README.md` | `911fa2d4945d57e40e096466dd86552f9ba3d13c` |
| Omnigent | `docs/AGENT_YAML_SPEC.md` | `6c017add9351c52c2aae75120f532ba6c8b23d5f` |
| Omnigent | `docs/POLICIES.md` | `1c15cde83eea891b187fa6232e752cdd6c08225e` |
| Ruflo | `main` | `e263c269dfb1eee948539c95121d1c816ca64629` |

When a contract test fails, diff against the SHA in this table. It is the record
of what the adapter was written to talk to.
