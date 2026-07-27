# Study: Ruflo

**Repository:** `ruvnet/ruflo` (formerly Claude Flow) · TypeScript · ~66.2k stars
**Pinned at:** `e263c269dfb1eee948539c95121d1c816ca64629`
**Primary sources read:** README, release history, ADR paths under `ruflo/docs/adr/`, community audits
**Role in Overmind:** vector memory across runs — and nothing else

---

## ⚠ Read this section first

This is the only study of the three where **the documentation cannot be taken as
a description of the software.** An [independent audit of
v3.5.51](https://github.com/ruvnet/ruflo/discussions/1513) tested every major
tool category and found that roughly **10 of 300+ advertised MCP tools actually
execute**; the remainder record JSON state and return success.

| Confirmed working | Confirmed stubbed |
| --- | --- |
| `memory_store` / `memory_search` (HNSW) | `agent_spawn` |
| `embeddings_generate` | `task_assign` |
| `session_save` | `neural_train` |
| `terminal_execute` | `workflow_execute` |
| | `wasm_agent_prompt` |

The audit is a community discussion post, not a peer-reviewed artifact, and
upstream disputes its framing. Treating one negative report as decisive would be
sloppy. But a separate [maintainer-thread user
report](https://github.com/ruvnet/ruflo/discussions/1666) independently describes
no measurable improvement in practice. **Two independent negative signals against
marketing claims is enough to design defensively**, and that is the entire basis
of Overmind's relationship with this dependency.

The stubbed list is not random. Everything that *executes* — spawning agents,
assigning tasks, running workflows — is stubbed. Everything that *stores* works.
This is a memory system with an orchestration façade.

## 1. Philosophy

Ruflo's stated belief is that agent capability is an emergent property of scale:
more agents, more tools, more topologies, more hooks, more neural components. The
README advertises 60–100+ agent types, 215–300+ MCP tools, swarm topologies
(Queen / mesh / hierarchical), consensus protocols, 27 lifecycle hooks, three
runtimes, SPARC methodology, federation with mTLS and PII stripping, and 3-tier
model routing claiming ~75% cost savings.

Its *operational* philosophy, visible in the release history, is ship-fast: 5,800+
commits and **1,488 releases** in roughly ten months, with 55 alphas before
v3.5.0 became the first stable release.

These two philosophies interact badly. Surface area grows faster than any process
can verify it, which is a sufficient mechanism to explain a 290-stub gap without
assuming bad faith. The lesson Overmind takes is not "Ruflo is bad" — it is that
**advertised surface area is not evidence of capability, and a composition layer
must verify each dependency's claims tool by tool.**

## 2. Architecture

As documented (treat the execution half as unverified):

```
  MCP surface  ── 215-300+ tools ─────────────────────────────┐
                                                              │
  ┌────────────────────────── VERIFIED ──────────────────────┐│
  │  AgentDB / HNSW vector index                             ││
  │  memory_store · memory_search · embeddings_generate       ││
  │  session_save                                             ││
  └───────────────────────────────────────────────────────────┘│
                                                              │
  ┌────────────────────── UNVERIFIED / STUBBED ──────────────┐│
  │  Queen orchestrator · topologies · consensus              ││
  │  agent_spawn · task_assign · workflow_execute             ││
  │  neural_train · SONA · ReasoningBank · wasm agents        ││
  │  27 hooks · federation (mTLS, PII stripping)              ││
  └───────────────────────────────────────────────────────────┘│
                                                              │
  Install: npx ruflo@latest init wizard                       │
  MCP:     claude mcp add ruflo -- npx ruflo@latest mcp start ─┘
```

The HNSW vector index (AgentDB) is the real asset. Approximate nearest-neighbour
search over stored agent experience is genuinely useful and genuinely tedious to
build well — which is precisely why depending on it is the right call even when
the surrounding claims do not hold.

## 3. Modules

Only four names matter to Overmind, and they are hardcoded as an allowlist in
`overmind/memory.py`:

| Tool | Use in Overmind |
| --- | --- |
| `memory_store` | Persist decisions (`overmind/decisions`) and failures (`overmind/failures`) |
| `memory_search` | Recall prior context before planning; verify carry-over on resume |
| `embeddings_generate` | Vectors for `semantic.loop_detect_semantic` and restatement fidelity |
| `session_save` | Session boundary persistence |

Any other tool name raises `RufloToolNotAllowed`. `terminal_execute` is
**deliberately excluded despite working**, because Omnigent's sandboxed execution
is strictly better and admitting a second execution path would fracture the
security model.

Documentation lives under a nested `ruflo/docs/adr/` prefix — `ADR-001` extension
architecture, `ADR-014` chat system, `ADR-018` E2E testing — not at the
repository root, which is worth knowing before searching for it.

## 4. Flow (as Overmind uses it)

1. `RufloMemory` starts `npx ruflo@latest mcp start` as a subprocess and speaks
   MCP over stdio, as a context manager so the process is always reaped.
2. `.call(tool, args)` checks the name against `ALLOWED` **before** dispatch. A
   disallowed name never reaches the wire.
3. `.recall(query, limit)` runs `memory_search` for prior decisions, bounded by
   `recall_limit` (default 12) so recall cannot flood the planner's context.
4. `.record_decision()` / `.record_failure()` write to their namespaces.
5. Unavailability raises `MemoryUnavailable`. **Overmind degrades rather than
   dies:** a run without memory loses cross-run learning but still plans,
   executes, gates, and integrates.

That last property is deliberate and is why `semantic.py` ships a pure-Python
character-n-gram cosine fallback. A gate that silently stops working when a
subprocess fails to start is worse than no gate, because the run still reports
green.

## 5. What Overmind takes, and what it must not take

**Takes:** four tools. HNSW-backed vector memory and embeddings.

**Takes as a warning:** the entire orchestration half of this repository is the
negative example Overmind is built against. 66,000 stars, 300 tools, ~10 working.
A gate that returns `{"status": "success"}` without checking anything is
indistinguishable from a working gate *until you audit it* — which is the reason
Overmind's own gates must fail on measured evidence (git state, exit codes) and
not on model self-report, and the reason `Similarity.source` reports `ngram` vs
`ruflo-embeddings` so a degraded measurement is never mistaken for a strong one.

**Must not take:** `agent_spawn`, `task_assign`, `workflow_execute`, swarm
topologies, consensus, Queen orchestration, SPARC, neural training, hooks,
federation. Some are stubs; the rest are the wrong architecture for this project.
Overmind has exactly one topology — a DAG with compulsory verification nodes.

**Must not take:** `terminal_execute`, despite it working.

**Risk and mitigation:** 1,488 releases in ten months is the highest churn of the
three dependencies. Overmind pins the version and runs a contract job in CI
(`upstream-contracts`, `continue-on-error: true`) that exercises the four
allowlisted tools and reports breakage loudly without failing unrelated builds.
Widening the allowlist is one line plus a test if upstream ships real backends —
and the audit should be repeated before that line is written.
