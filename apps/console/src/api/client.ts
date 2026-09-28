import {
  ConsoleContractError,
  decodeActivityFeed,
  decodeAgents,
  decodeContexts,
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
    limit?: number
    eventTypes?: string[]
    entityTypes?: string[]
  } = {}): Promise<ActivityFeedReadModel> {
    return decodeActivityFeed(
      await this.request('/console/activity', {
        method: 'POST',
        body: JSON.stringify({
          since: options.since ?? 0,
          limit: options.limit ?? 100,
          event_types: options.eventTypes ?? [],
          entity_types: options.entityTypes ?? [],
        }),
      }),
    )
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
