import { ConsoleOperationError, decodeAccessMeshSlot, decodeAccessMeshSlots, decodeAccessMeshDefaults, decodeSlotMutation, publicErrorCode } from './slotContracts'
import {
  ConsoleContractError,
  decodeActivityFeed,
  decodeAgents,
  decodeContexts,
  decodeManagedFleetControl,
  decodeNodeRuntime,
  decodeRuntimeOperation,
  decodeSnapshot,
  decodeTaskDetail,
  decodeTasks,
  decodeWebSocketTicket,
} from './contracts'
import type {
  AccessMeshDefaultsReadModel,
  AccessMeshMutationRequest,
  AccessMeshSlotReadModel,
  ActivityFeedReadModel,
  AgentCollectionReadModel,
  ConsoleSnapshotReadModel,
  ContextCollectionReadModel,
  ManagedFleetControlReadModel,
  ManagedFleetEnrollment,
  ManagedFleetMutationResult,
  MeshVpnResult,
  MeshVpnStatus,
  NodeRuntimeTarget,
  AccessMutationResult,
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
    readonly endpoint?: string,
    readonly elapsedMs?: number,
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


export class ConsoleClient {
  private readonly origin: string

  constructor(
    origin: string,
    private readonly accessToken: () => string,
    private readonly fetcher: FetchLike = fetch,
    private readonly timeoutMs = 20_000,
  ) {
    this.origin = new URL(origin).origin
    if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) throw new Error('invalid_request_timeout')
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

  async accessMeshDefaults(): Promise<AccessMeshDefaultsReadModel> {
    return decodeAccessMeshDefaults(await this.request('/actions/access/defaults'))
  }

  async accessMeshSlot(slotId: string): Promise<AccessMeshSlotReadModel> {
    const slot = decodeAccessMeshSlot(await this.request('/actions/access/slots/' + encodeURIComponent(slotId)))
    if (slot.slotId !== slotId) throw new ConsoleContractError('$.slot.slot_id', 'response belongs to another slot')
    return slot
  }

  async accessMeshSlots(options: { limit?: number; cursor?: string } = {}) {
    const params = new URLSearchParams({ limit: String(options.limit ?? 100) })
    if (options.cursor) params.set('cursor', options.cursor)
    return decodeAccessMeshSlots(await this.request('/actions/access/slots?' + params.toString()))
  }

  async accessMeshAllSlots(): Promise<AccessMeshSlotReadModel[]> {
    const slots: AccessMeshSlotReadModel[] = []
    const cursors = new Set<string>()
    const ids = new Set<string>()
    let cursor: string | undefined
    for (let page = 0; page < 100; page += 1) {
      const next = await this.accessMeshSlots({ limit: 100, cursor })
      for (const slot of next.slots) {
        const key = slot.issuerNodeId + '/' + slot.slotId
        if (ids.has(key)) throw new ConsoleContractError('$.slots', 'duplicate slot across pages; refresh required')
        ids.add(key)
        slots.push(slot)
      }
      if (next.nextCursor === null) return slots
      if (cursors.has(next.nextCursor)) throw new ConsoleContractError('$.next_cursor', 'cursor did not advance')
      cursors.add(next.nextCursor)
      cursor = next.nextCursor
    }
    throw new ConsoleContractError('$.next_cursor', 'slot page limit reached')
  }

  async accessMeshMutation(body: AccessMeshMutationRequest): Promise<AccessMutationResult> {
    return decodeSlotMutation(await this.request('/actions/access/mutate', {
      method: 'POST', body: JSON.stringify(body),
    }))
  }

  async meshVpnStatus(): Promise<MeshVpnStatus> {
    const raw = await this.request('/actions/fleet/control/transport')
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
      throw new ConsoleContractError('$', 'Mesh VPN status was not an object')
    }
    const item = raw as Record<string, unknown>
    if (item.ok !== true || typeof item.prepared !== 'boolean') {
      throw new ConsoleContractError('$.prepared', 'invalid Mesh VPN status')
    }
    return item as MeshVpnStatus
  }

  async meshVpnMutation(
    action: 'prepare' | 'enroll' | 'activate' | 'backend' | 'switch' | 'revoke',
    body: Record<string, unknown> = {},
  ): Promise<MeshVpnResult> {
    const raw = await this.request('/actions/fleet/control/transport/' + action, {
      method: 'POST', body: JSON.stringify(body),
    })
    if (!raw || typeof raw !== 'object' || Array.isArray(raw) || typeof (raw as Record<string, unknown>).ok !== 'boolean') {
      throw new ConsoleContractError('$', 'invalid Mesh VPN mutation receipt')
    }
    return raw as MeshVpnResult
  }

  async fleetControl(): Promise<ManagedFleetControlReadModel> {
    const raw = await this.request('/actions/fleet/control')
    if (raw && typeof raw === 'object' && !Array.isArray(raw) && (raw as Record<string, unknown>).ok === false) {
      throw new ConsoleOperationError(publicErrorCode(raw))
    }
    return decodeManagedFleetControl(raw)
  }

  async nodeRuntime() {
    return decodeNodeRuntime(await this.request('/actions/fleet/control/runtime'))
  }

  async nodeRuntimeOperation(requestId: string) {
    const raw = await this.request('/actions/fleet/control/runtime/operations/' + encodeURIComponent(requestId))
    const operation = decodeRuntimeOperation((raw as Record<string, unknown>).operation)
    if (operation.request_id !== requestId) throw new ConsoleContractError('$.operation.request_id', 'receipt belongs to another request')
    return operation
  }

  async restartNodeRuntime(target: NodeRuntimeTarget, requestId: string) {
    const raw = await this.request('/actions/fleet/control/runtime/restart', {
      method: 'POST', body: JSON.stringify({ target, request_id: requestId }),
    })
    const operation = decodeRuntimeOperation((raw as Record<string, unknown>).operation)
    if (operation.request_id !== requestId || operation.target !== target) throw new ConsoleContractError('$.operation', 'receipt belongs to another request')
    return operation
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
    const controller = new AbortController()
    const startedAt = Date.now()
    // Diagnostic route templates contain neither credentials, payloads nor selected slot IDs.
    const endpoint = new URL(path, this.origin).pathname.replace(
      /^\/actions\/access\/slots\/[^/]+$/, '/actions/access/slots/:slot_id',
    )
    const failure = (code: string) => new ConsoleHttpError(
      0, code, 'Terminal MCP Console request failed', endpoint, Date.now() - startedAt,
    )
    let timer: ReturnType<typeof setTimeout> | undefined
    let stop: (error: ConsoleHttpError) => void = () => undefined
    const deadline = new Promise<never>((_resolve, reject) => {
      stop = reject
      timer = setTimeout(() => {
        reject(failure('request_timeout'))
        controller.abort()
      }, this.timeoutMs)
    })
    const cancelled = () => {
      stop(failure('request_cancelled'))
      controller.abort()
    }
    if (init.signal?.aborted) cancelled()
    else init.signal?.addEventListener('abort', cancelled, { once: true })

    const perform = async () => {
      if (controller.signal.aborted) throw failure('request_cancelled')
      let response: Response
      try {
        response = await this.fetcher(new URL(path, this.origin), {
          ...init, signal: controller.signal,
          headers: {
            Accept: 'application/json',
            Authorization: `Bearer ${this.accessToken()}`,
            ...(init.body ? { 'Content-Type': 'application/json' } : {}),
            ...init.headers,
          },
        })
      } catch {
        throw failure('network_error')
      }
      const body = await payload(response)
      if (!response.ok) {
        const code = publicErrorCode(body)
        throw new ConsoleHttpError(response.status, code === 'request_failed' ? 'http_' + response.status : code, undefined,
          endpoint, Date.now() - startedAt)
      }
      return body
    }
    try {
      // Race also bounds native bridges and body streams that ignore AbortSignal.
      // A timed-out mutation is uncertain: this transport never retries it.
      return await Promise.race([perform(), deadline])
    } finally {
      clearTimeout(timer)
      init.signal?.removeEventListener('abort', cancelled)
    }
  }
}
