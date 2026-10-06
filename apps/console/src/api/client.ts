import {
  ConsoleContractError,
  decodeActivityFeed,
  decodeAgents,
  decodeContexts,
  decodeManagedFleetControl,
  decodeSnapshot,
  decodeTaskDetail,
  decodeTasks,
  decodeWebSocketTicket,
} from './contracts'
import type {
  ActivityFeedReadModel,
  AgentCollectionReadModel,
  ConsoleSnapshotReadModel,
  ContextCollectionReadModel,
  ManagedFleetControlReadModel,
  ManagedFleetEnrollment,
  ManagedFleetMutationResult,
  PersistentMutationResult,
  TaskCollectionReadModel,
  TaskReadModel,
  WebSocketTicketReadModel,
} from './models'

export type FetchLike = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>

export class ConsoleHttpError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message = 'Terminal MCP Console request failed',
  ) {
    super(message)
    this.name = 'ConsoleHttpError'
  }

  get authenticationRequired(): boolean {
    return this.status === 401
  }

  get retryable(): boolean {
    return this.status === 0 || this.status === 429 || this.status >= 500
  }
}

async function payload(response: Response): Promise<unknown> {
  try {
    return await response.json()
  } catch {
    if (response.ok) throw new ConsoleContractError('$', 'response was not JSON')
    return {}
  }
}

function errorCode(value: unknown): string {
  if (value && typeof value === 'object' && 'error' in value) {
    const code = (value as { error?: unknown }).error
    if (typeof code === 'string') return code
  }
  return 'request_failed'
}

export class ConsoleClient {
  private readonly origin: string

  constructor(
    origin: string,
    private readonly accessToken: () => string,
    private readonly fetcher: FetchLike = fetch,
  ) {
    this.origin = new URL(origin).origin
  }

  async snapshot(): Promise<ConsoleSnapshotReadModel> {
    return decodeSnapshot(await this.request('/actions/console/snapshot'))
  }

  async agents(): Promise<AgentCollectionReadModel> {
    return decodeAgents(
      await this.request('/actions/agents', {
        method: 'POST',
        body: JSON.stringify({
          show_details: true,
          show_intents: false,
          show_commands: false,
          since_minutes: 60,
        }),
      }),
    )
  }

  async tasks(): Promise<TaskCollectionReadModel> {
    return decodeTasks(
      await this.request('/actions/tasks', {
        method: 'POST',
        body: JSON.stringify({
          show_details: false,
          show_done: true,
          show_archived: true,
          limit: 200,
          cursor: 0,
        }),
      }),
    )
  }

  async task(namespace: string, taskId: string): Promise<TaskReadModel> {
    return decodeTaskDetail(
      await this.request('/console/task-detail', {
        method: 'POST',
        body: JSON.stringify({ namespace, task_id: taskId }),
      }),
    )
  }

  async activity(options: {
    since?: number
    before?: number
    limit?: number
    eventTypes?: string[]
    entityTypes?: string[]
  } = {}): Promise<ActivityFeedReadModel> {
    return decodeActivityFeed(
      await this.request('/console/activity', {
        method: 'POST',
        body: JSON.stringify({
          since: options.since ?? 0,
          before: options.before,
          limit: options.limit ?? 100,
          event_types: options.eventTypes ?? [],
          entity_types: options.entityTypes ?? [],
        }),
      }),
    )
  }

  async persistentMutation(path: string, body: Record<string, unknown>): Promise<PersistentMutationResult> {
    if (!path.startsWith('/actions/persistent/')) throw new Error('invalid_persistent_path')
    const raw = await this.request(path, { method: 'POST', body: JSON.stringify(body) })
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new ConsoleContractError('$', 'persistent mutation response was not an object')
    const payload = raw as Record<string, unknown>
    if (typeof payload.ok !== 'boolean') throw new ConsoleContractError('$.ok', 'expected boolean')
    return {
      ok: payload.ok,
      code: typeof payload.code === 'string' ? payload.code : undefined,
      error: typeof payload.error === 'string' ? payload.error : undefined,
      blockers: Array.isArray(payload.blockers) ? payload.blockers.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object' && !Array.isArray(item)) : undefined,
      payload,
    }
  }

  async fleetControl(): Promise<ManagedFleetControlReadModel> {
    return decodeManagedFleetControl(await this.request('/actions/fleet/control'))
  }

  async fleetEnrollment(): Promise<ManagedFleetEnrollment> {
    const raw = await this.request('/actions/fleet/control/enrollment')
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
      throw new ConsoleContractError('$', 'fleet enrollment response was not an object')
    }
    const payload = raw as Record<string, unknown>
    const enrollment = payload.enrollment
    if (!enrollment || typeof enrollment !== 'object' || Array.isArray(enrollment)) {
      throw new ConsoleContractError('$.enrollment', 'expected object')
    }
    const item = enrollment as Record<string, unknown>
    for (const key of ['node_id', 'origin', 'public_key', 'auth_token'] as const) {
      if (typeof item[key] !== 'string' || item[key].length === 0) {
        throw new ConsoleContractError('$.enrollment.' + key, 'expected non-empty string')
      }
    }
    return {
      nodeId: item.node_id as string,
      origin: item.origin as string,
      publicKey: item.public_key as string,
      authToken: item.auth_token as string,
    }
  }

  async fleetControlMutation(
    path: string,
    body: Record<string, unknown> = {},
  ): Promise<ManagedFleetMutationResult> {
    if (!path.startsWith('/actions/fleet/control/')) throw new Error('invalid_fleet_control_path')
    const raw = await this.request(path, { method: 'POST', body: JSON.stringify(body) })
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
      throw new ConsoleContractError('$', 'fleet control response was not an object')
    }
    const payload = raw as Record<string, unknown>
    if (typeof payload.ok !== 'boolean') throw new ConsoleContractError('$.ok', 'expected boolean')
    let mutation: ManagedFleetMutationResult['mutation']
    if (payload.mutation !== undefined) {
      if (!payload.mutation || typeof payload.mutation !== 'object' || Array.isArray(payload.mutation)) {
        throw new ConsoleContractError('$.mutation', 'expected object')
      }
      const item = payload.mutation as Record<string, unknown>
      if (item.status !== 'committed') {
        throw new ConsoleContractError('$.mutation.status', 'expected committed')
      }
      if (item.convergence !== 'pending' && item.convergence !== 'converged') {
        throw new ConsoleContractError('$.mutation.convergence', 'expected pending or converged')
      }
      if (!Number.isInteger(item.topology_revision) || (item.topology_revision as number) < 0) {
        throw new ConsoleContractError('$.mutation.topology_revision', 'expected non-negative integer')
      }
      mutation = {
        status: item.status,
        convergence: item.convergence,
        topologyRevision: item.topology_revision as number,
      }
    }
    return {
      ok: payload.ok,
      code: typeof payload.code === 'string' ? payload.code : undefined,
      error: typeof payload.error === 'string' ? payload.error : undefined,
      control: payload.control ? decodeManagedFleetControl({ control: payload.control }) : undefined,
      mutation,
    }
  }

  async webSocketTicket(): Promise<WebSocketTicketReadModel> {
    return decodeWebSocketTicket(
      await this.request('/console/ws-ticket', {
        method: 'POST',
      }),
    )
  }

  async contexts(): Promise<ContextCollectionReadModel> {
    return decodeContexts(
      await this.request('/actions/context', {
        method: 'POST',
        body: JSON.stringify({ action: 'list', show_details: true }),
      }),
    )
  }

  private async request(path: string, init: RequestInit = {}): Promise<unknown> {
    let response: Response
    try {
      response = await this.fetcher(new URL(path, this.origin), {
        ...init,
        headers: {
          Accept: 'application/json',
          Authorization: `Bearer ${this.accessToken()}`,
          ...(init.body ? { 'Content-Type': 'application/json' } : {}),
          ...init.headers,
        },
      })
    } catch {
      throw new ConsoleHttpError(0, 'network_error')
    }

    const body = await payload(response)
    if (!response.ok) {
      throw new ConsoleHttpError(response.status, errorCode(body))
    }
    return body
  }
}
