/**
 * Plan validation at the process boundary.
 *
 * This file exists because the previous `normalise()` repaired what it could not
 * understand: an unrecognised `exit_check.kind` became `tests_pass`, a
 * `command_exit_zero` with no command became `tests_pass`, and a missing role
 * became `implementer`. Each of those turns a model mistake into a plan that
 * looks valid, and the run then spends real money on exit conditions nobody
 * chose -- or grants write access to a task that was meant to be read-only.
 *
 * The rule here: normalise *shape* (trim, dedupe, sort, slugify), never
 * normalise *meaning*. Anything semantic that is missing or unrecognised is an
 * issue with the field path attached, and the caller decides what to do.
 *
 * Zero dependencies on purpose. This is the only TypeScript in the project and
 * a validator that needs a package tree is a validator that rots.
 */

export type ExitKind =
  | 'tests_pass'
  | 'build_succeeds'
  | 'schema_valid'
  | 'command_exit_zero'
  | 'diff_nonempty'

export interface TaskNode {
  id: string
  role: string
  intent: string
  acceptance: string
  reads: string[]
  writes: string[]
  depends_on: string[]
  exit_check: { kind: ExitKind; command?: string }
}

/** One rejection. `field` is a JSON path the operator can go look at. */
export interface ValidationIssue {
  field: string
  message: string
}

export interface ValidationResult {
  nodes: TaskNode[]
  issues: ValidationIssue[]
}

/** Mirrors overmind/models.py ExitKind. MODEL_ASSERTION is deliberately absent:
 *  linearity.validate rejects non-machine-checkable exits, so accepting one here
 *  would only move the failure later. */
export const VALID_EXITS = new Set<string>([
  'tests_pass',
  'build_succeeds',
  'schema_valid',
  'command_exit_zero',
  'diff_nonempty',
])

/** Mirrors overmind/models.py Role, minus planner: the coordinator IS the
 *  planner, so a task claiming that role is a confused plan. Verifier tasks are
 *  inserted by linearity.py, not requested. */
export const VALID_ROLES = new Set<string>(['researcher', 'implementer', 'verifier', 'reviewer'])

const MAX_ID = 48

/** Shape-only. Never invents an id: a task without one is an issue. */
export function slug(value: string): string {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, MAX_ID)
}

export function paths(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return [
    ...new Set(value.filter((v): v is string => typeof v === 'string' && v.trim().length > 0)),
  ]
    .map((v) => v.trim())
    .sort()
}

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

/**
 * Validate one task. Returns the node when it is usable, plus every issue found
 * -- all of them, not the first, so one round trip reports the whole story.
 */
export function validateTask(raw: Record<string, unknown>, index: number): ValidationResult {
  const at = `tasks[${index}]`
  const issues: ValidationIssue[] = []

  const rawId = text(raw.id)
  const id = rawId ? slug(rawId) : ''
  if (!id) {
    issues.push({
      field: `${at}.id`,
      message: 'missing or unusable id; ids are how depends_on refers to tasks',
    })
  }

  const role = text(raw.role).toLowerCase()
  if (!role) {
    issues.push({ field: `${at}.role`, message: 'missing role' })
  } else if (!VALID_ROLES.has(role)) {
    issues.push({
      field: `${at}.role`,
      message: `unknown role ${JSON.stringify(role)}; expected one of ${[...VALID_ROLES].join(', ')}`,
    })
  }

  const intent = text(raw.intent) || text(raw.description)
  if (!intent) {
    issues.push({ field: `${at}.intent`, message: 'missing intent' })
  }

  const acceptance = text(raw.acceptance)
  if (!acceptance) {
    issues.push({
      field: `${at}.acceptance`,
      message: 'missing acceptance; a task nobody else can check cannot be verified',
    })
  }

  const reads = paths(raw.reads)
  const writes = paths(raw.writes)
  if (role === 'implementer' && writes.length === 0) {
    issues.push({
      field: `${at}.writes`,
      message: 'an implementer that declares no writes produces nothing checkable',
    })
  }

  const dependsOn = paths(raw.depends_on).map(slug)
  if (id && dependsOn.includes(id)) {
    issues.push({ field: `${at}.depends_on`, message: `task ${id} depends on itself` })
  }

  const rawExit = (raw.exit_check ?? {}) as { kind?: unknown; command?: unknown }
  const kind = text(rawExit.kind)
  const command = text(rawExit.command)

  if (!kind) {
    issues.push({
      field: `${at}.exit_check.kind`,
      message: `missing exit_check.kind; expected one of ${[...VALID_EXITS].join(', ')}`,
    })
  } else if (!VALID_EXITS.has(kind)) {
    issues.push({
      field: `${at}.exit_check.kind`,
      message: `unknown exit kind ${JSON.stringify(kind)}; expected one of ${[...VALID_EXITS].join(', ')}`,
    })
  } else if (kind === 'command_exit_zero' && !command) {
    issues.push({
      field: `${at}.exit_check.command`,
      message: 'command_exit_zero requires the command to run; there is nothing to check without it',
    })
  }

  if (issues.length > 0) return { nodes: [], issues }

  return {
    nodes: [
      {
        id,
        role,
        intent,
        acceptance,
        reads,
        writes,
        depends_on: dependsOn,
        exit_check: command ? { kind: kind as ExitKind, command } : { kind: kind as ExitKind },
      },
    ],
    issues: [],
  }
}

/** Validate the whole DAG, including the cross-task checks a single task cannot see. */
export function validatePlan(rawTasks: Record<string, unknown>[]): ValidationResult {
  const issues: ValidationIssue[] = []
  const nodes: TaskNode[] = []

  if (rawTasks.length === 0) {
    return { nodes: [], issues: [{ field: 'tasks', message: 'the coordinator returned no tasks' }] }
  }

  rawTasks.forEach((raw, index) => {
    const result = validateTask(raw, index)
    issues.push(...result.issues)
    nodes.push(...result.nodes)
  })

  const seen = new Map<string, number>()
  nodes.forEach((node, index) => {
    const first = seen.get(node.id)
    if (first !== undefined) {
      issues.push({
        field: `tasks[${index}].id`,
        message: `duplicate id ${JSON.stringify(node.id)}, already used by tasks[${first}]`,
      })
      return
    }
    seen.set(node.id, index)
  })

  // Dangling dependency: the Python side would drop the edge and silently run
  // the task early, which is exactly the ordering bug context_carry catches
  // after the money is spent.
  nodes.forEach((node, index) => {
    for (const dep of node.depends_on) {
      if (!seen.has(dep)) {
        issues.push({
          field: `tasks[${index}].depends_on`,
          message: `${JSON.stringify(dep)} is not the id of any task in this plan`,
        })
      }
    }
  })

  return issues.length > 0 ? { nodes: [], issues } : { nodes, issues: [] }
}

/** Render issues for a retry prompt. Field paths included: the coordinator fixes
 *  what it is told is broken, and 'invalid plan' tells it nothing. */
export function describeIssues(issues: ValidationIssue[]): string {
  return issues.map((issue) => `  - ${issue.field}: ${issue.message}`).join('\n')
}
