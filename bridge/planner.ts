/**
 * The OMA coordinator, exposed as a local HTTP service.
 *
 * This is the whole TypeScript surface of the project. It does not plan --
 * @open-multi-agent/core plans. It translates one goal into OMA's dynamic
 * task-DAG planning and normalises the result into the shape overmind/models.py
 * validates.
 *
 * Everything Overmind adds to the plan (verification interleaving, conflict
 * serialization, vendor routing) happens on the Python side, deliberately:
 * the rewrite must be independent of whichever planner produced the DAG.
 */

import { createServer, type IncomingMessage, type ServerResponse } from 'node:http'
import { OpenMultiAgent } from '@open-multi-agent/core'

const PORT = Number(process.env.OVERMIND_BRIDGE_PORT ?? 7801)
const MODEL = process.env.OVERMIND_PLANNER_MODEL ?? 'gpt-5.4'
const PROVIDER = process.env.OVERMIND_PLANNER_PROVIDER ?? 'openai'

type ExitKind =
  | 'tests_pass'
  | 'build_succeeds'
  | 'schema_valid'
  | 'command_exit_zero'
  | 'diff_nonempty'

interface PlanRequest {
  goal: string
  contract: string
  prior_decisions?: string[]
  max_parallel_hint?: number
}

interface TaskNode {
  id: string
  role: string
  intent: string
  acceptance: string
  reads: string[]
  writes: string[]
  depends_on: string[]
  exit_check: { kind: ExitKind; command?: string }
}

const oma = new OpenMultiAgent({ defaultProvider: PROVIDER, defaultModel: MODEL })

/** Prior decisions come from Ruflo recall. Injected, not appended, so the
 *  coordinator treats them as constraints rather than trivia. */
function priorBlock(prior: string[]): string {
  if (prior.length === 0) return ''
  const lines = prior.map((d, i) => `  ${i + 1}. ${d}`).join('\n')
  return [
    '',
    'PRIOR DECISIONS from earlier runs in this repository.',
    'Treat these as settled. Contradicting one is allowed only if you say why.',
    lines,
    '',
  ].join('\n')
}

function slug(value: unknown, fallback: string): string {
  const raw = typeof value === 'string' && value.trim() ? value : fallback
  return raw
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 48) || fallback
}

function paths(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return [...new Set(value.filter((v): v is string => typeof v === 'string' && v.length > 0))].sort()
}

const VALID_EXITS = new Set<ExitKind>([
  'tests_pass',
  'build_succeeds',
  'schema_valid',
  'command_exit_zero',
  'diff_nonempty',
])

/**
 * Normalise one coordinator task.
 *
 * A missing or unrecognised exit kind becomes tests_pass rather than being
 * passed through: the Python validator rejects non-machine-checkable exits and
 * failing here with a clear default is cheaper than failing there.
 */
function normalise(raw: Record<string, unknown>, index: number): TaskNode {
  const rawExit = (raw.exit_check ?? {}) as { kind?: string; command?: string }
  const kind = VALID_EXITS.has(rawExit.kind as ExitKind)
    ? (rawExit.kind as ExitKind)
    : 'tests_pass'

  const writes = paths(raw.writes)
  return {
    id: slug(raw.id, `task-${index + 1}`),
    role: typeof raw.role === 'string' ? raw.role : 'implementer',
    intent: String(raw.intent ?? raw.description ?? '').trim(),
    acceptance: String(raw.acceptance ?? '').trim(),
    reads: paths(raw.reads),
    writes,
    depends_on: paths(raw.depends_on).map((d) => slug(d, d)),
    exit_check:
      kind === 'command_exit_zero' && !rawExit.command
        ? { kind: 'tests_pass' }
        : { kind, command: rawExit.command },
  }
}

/** Pull the task array out of the coordinator output without assuming one shape. */
function extractTasks(output: unknown): Record<string, unknown>[] {
  const text = typeof output === 'string' ? output : JSON.stringify(output ?? {})
  const start = text.indexOf('{')
  const end = text.lastIndexOf('}')
  if (start < 0 || end <= start) return []
  try {
    const parsed = JSON.parse(text.slice(start, end + 1)) as Record<string, unknown>
    for (const key of ['tasks', 'nodes', 'plan', 'dag']) {
      const candidate = parsed[key]
      if (Array.isArray(candidate)) return candidate as Record<string, unknown>[]
    }
    return []
  } catch {
    return []
  }
}

function ambiguityOf(output: unknown): number {
  const text = typeof output === 'string' ? output : JSON.stringify(output ?? {})
  const match = text.match(/"ambiguity"\s*:\s*([0-9.]+)/)
  const value = match ? Number(match[1]) : 0
  return Number.isFinite(value) ? Math.min(Math.max(value, 0), 1) : 0
}

async function buildPlan(req: PlanRequest): Promise<{ nodes: TaskNode[]; ambiguity: number }> {
  const team = oma.createTeam('overmind-planning', {
    name: 'overmind-planning',
    agents: [
      {
        name: 'coordinator',
        systemPrompt: [
          'You decompose one engineering goal into a task DAG. You write no code.',
          req.contract,
          priorBlock(req.prior_decisions ?? []),
          `Aim for at most ${req.max_parallel_hint ?? 3} concurrent tasks.`,
          'Reply with a single JSON object: {"tasks": [...], "ambiguity": <0..1>}.',
        ].join('\n\n'),
      },
    ],
    sharedMemory: true,
  })

  const result = await oma.runTeam(team, req.goal)
  const output = result.agentResults.get('coordinator')?.output
  const tasks = extractTasks(output)

  if (tasks.length === 0) {
    throw new Error('coordinator returned no parseable tasks')
  }

  return { nodes: tasks.map(normalise), ambiguity: ambiguityOf(output) }
}

function body(req: IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    let data = ''
    req.on('data', (chunk) => {
      data += chunk
      if (data.length > 1_000_000) reject(new Error('request too large'))
    })
    req.on('end', () => resolve(data))
    req.on('error', reject)
  })
}

function json(res: ServerResponse, status: number, payload: unknown): void {
  const encoded = JSON.stringify(payload)
  res.writeHead(status, { 'content-type': 'application/json' })
  res.end(encoded)
}

const server = createServer(async (req, res) => {
  if (req.method === 'GET' && req.url === '/health') {
    json(res, 200, { ok: true, provider: PROVIDER, model: MODEL })
    return
  }

  if (req.method !== 'POST' || req.url !== '/plan') {
    json(res, 404, { error: 'not found' })
    return
  }

  try {
    const parsed = JSON.parse(await body(req)) as PlanRequest
    if (!parsed.goal?.trim()) {
      json(res, 400, { error: 'goal is required' })
      return
    }
    json(res, 200, await buildPlan(parsed))
  } catch (error) {
    json(res, 500, { error: error instanceof Error ? error.message : String(error) })
  }
})

server.listen(PORT, '127.0.0.1', () => {
  process.stdout.write(`overmind bridge listening on http://127.0.0.1:${PORT}\n`)
})
