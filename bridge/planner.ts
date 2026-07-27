/**
 * The OMA coordinator, exposed as a local HTTP service.
 *
 * This is the whole TypeScript surface of the project. It does not plan --
 * @open-multi-agent/core plans. It translates one goal into OMA's dynamic
 * task-DAG planning and hands back a plan that overmind/models.py can validate.
 *
 * Everything Overmind adds to the plan (verification interleaving, conflict
 * serialization, vendor routing) happens on the Python side, deliberately: the
 * rewrite must be independent of whichever planner produced the DAG.
 *
 * What this file no longer does is repair the coordinator's output. See
 * schema.ts: bad plans are rejected with field paths, retried once with those
 * paths fed back, and then refused with 422.
 */

import { createServer, type IncomingMessage, type ServerResponse } from 'node:http'
import { OpenMultiAgent } from '@open-multi-agent/core'
import {
  describeIssues,
  validatePlan,
  type TaskNode,
  type ValidationIssue,
} from './schema.js'

const PORT = Number(process.env.OVERMIND_BRIDGE_PORT ?? 7801)
const MODEL = process.env.OVERMIND_PLANNER_MODEL ?? 'gpt-5.4'
const PROVIDER = process.env.OVERMIND_PLANNER_PROVIDER ?? 'openai'

/** One. A model that omitted a field usually supplies it when told which field;
 *  a model that fails twice has a prompt or capability problem, and retrying it
 *  burns tokens to arrive at the same 422. */
const MAX_ATTEMPTS = 2

interface PlanRequest {
  goal: string
  contract: string
  prior_decisions?: string[]
  max_parallel_hint?: number
}

interface PlanResponse {
  nodes: TaskNode[]
  ambiguity: number
  attempts: number
}

class PlanInvalid extends Error {
  constructor(
    readonly issues: ValidationIssue[],
    readonly attempts: number,
  ) {
    super(`plan failed validation after ${attempts} attempt(s)`)
    this.name = 'PlanInvalid'
  }
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

function systemPrompt(req: PlanRequest, correction: string): string {
  return [
    'You decompose one engineering goal into a task DAG. You write no code.',
    req.contract,
    priorBlock(req.prior_decisions ?? []),
    `Aim for at most ${req.max_parallel_hint ?? 3} concurrent tasks.`,
    'Reply with a single JSON object: {"tasks": [...], "ambiguity": <0..1>}.',
    correction,
  ]
    .filter((part) => part.length > 0)
    .join('\n\n')
}

function correctionBlock(issues: ValidationIssue[]): string {
  return [
    'YOUR PREVIOUS REPLY WAS REJECTED. Fix exactly these fields and reply again',
    'with the complete task list. Do not drop the tasks that were fine.',
    describeIssues(issues),
  ].join('\n')
}

async function attempt(req: PlanRequest, correction: string) {
  const team = oma.createTeam('overmind-planning', {
    name: 'overmind-planning',
    agents: [{ name: 'coordinator', systemPrompt: systemPrompt(req, correction) }],
    sharedMemory: true,
  })

  const result = await oma.runTeam(team, req.goal)
  const output = result.agentResults.get('coordinator')?.output
  return { validated: validatePlan(extractTasks(output)), ambiguity: ambiguityOf(output) }
}

async function buildPlan(req: PlanRequest): Promise<PlanResponse> {
  let correction = ''
  let issues: ValidationIssue[] = []

  for (let n = 1; n <= MAX_ATTEMPTS; n += 1) {
    const { validated, ambiguity } = await attempt(req, correction)

    if (validated.issues.length === 0) {
      return { nodes: validated.nodes, ambiguity, attempts: n }
    }

    issues = validated.issues
    correction = correctionBlock(issues)
    process.stdout.write(
      `attempt ${n} rejected:\n${describeIssues(issues)}\n${
        n < MAX_ATTEMPTS ? 'retrying once with the errors fed back\n' : ''
      }`,
    )
  }

  throw new PlanInvalid(issues, MAX_ATTEMPTS)
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
    // 422, not 500: the service worked, the plan is unacceptable. The client
    // distinguishes these, and so should anyone reading the log.
    if (error instanceof PlanInvalid) {
      json(res, 422, {
        error: error.message,
        attempts: error.attempts,
        issues: error.issues,
      })
      return
    }
    json(res, 500, { error: error instanceof Error ? error.message : String(error) })
  }
})

server.listen(PORT, '127.0.0.1', () => {
  process.stdout.write(`overmind bridge listening on http://127.0.0.1:${PORT}\n`)
})
