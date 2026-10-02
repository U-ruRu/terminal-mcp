import { ConsoleClient, type FetchLike } from '../api/client'
import type { ActivityFeedReadModel, PersistentMutationResult, TaskReadModel } from '../api/models'
import { ConnectionManager } from '../auth/pairing'
import { PairingTransport } from '../auth/transport'
import { BrowserCredentialVault, type KeyValueStorage } from '../auth/vault'
import type { ConnectionProfile, ProfileRestoreResult } from '../connections/types'
import {
  RealtimeConsoleEngine,
  type RealtimeEngineOptions,
  type RealtimeScheduler,
  type RealtimeSocketFactory,
} from '../realtime/engine'
import { boundedBackoffDelay } from '../realtime/state'
import { DEFAULT_FLEET_REQUEST_TIMEOUT_MS, withRequestTimeout } from './policy'
import type { FleetActivityOptions, FleetInstanceActor, FleetInstanceRuntimeState } from './types'

type Listener = (state: FleetInstanceRuntimeState) => void

const browserScheduler: RealtimeScheduler = {
  setTimeout: (callback, delayMs) => window.setTimeout(callback, delayMs),
  clearTimeout: (handle) => window.clearTimeout(handle as number),
}

export function jitterDelay(delayMs: number, random: () => number = Math.random): number {
  const sample = Math.max(0, Math.min(1, random()))
  return Math.max(0, Math.round(delayMs * (0.8 + sample * 0.4)))
}

type FleetActorCredentialSource = {
  restore(instanceId: string): Promise<ProfileRestoreResult>
  invalidateAccessSession?(instanceId: string): void
}

export type BrowserFleetActorOptions = {
  credentialSource?: FleetActorCredentialSource
  storage?: KeyValueStorage
  fetcher?: FetchLike
  socketFactory?: RealtimeSocketFactory
  scheduler?: RealtimeScheduler
  random?: () => number
  reconnectBaseMs?: number
  reconnectMaxMs?: number
  requestTimeoutMs?: number
}

export class BrowserFleetInstanceActor implements FleetInstanceActor {
  readonly instanceId: string

  private readonly listeners = new Set<Listener>()
  private readonly scheduler: RealtimeScheduler
  private readonly random: () => number
  private readonly reconnectBaseMs: number
  private readonly reconnectMaxMs: number
  private readonly auth: ConnectionManager | null
  private readonly credentialSource?: FleetActorCredentialSource
  private readonly fetcher: FetchLike
  private readonly engineOptions: RealtimeEngineOptions
  private state: FleetInstanceRuntimeState
  private engine: RealtimeConsoleEngine | null = null
  private client: ConsoleClient | null = null
  private engineUnsubscribe: (() => void) | null = null
  private authRetryTimer: unknown = null
  private authAttempt = 0
  private accessToken = ''
  private running = false
  private generation = 0

  constructor(
    private readonly profile: ConnectionProfile,
    options: BrowserFleetActorOptions = {},
  ) {
    this.instanceId = profile.instanceId
    this.scheduler = options.scheduler ?? browserScheduler
    this.random = options.random ?? Math.random
    this.reconnectBaseMs = options.reconnectBaseMs ?? 500
    this.reconnectMaxMs = options.reconnectMaxMs ?? 15_000
    this.fetcher = withRequestTimeout(
      options.fetcher ?? fetch,
      options.requestTimeoutMs ?? DEFAULT_FLEET_REQUEST_TIMEOUT_MS,
      this.scheduler,
    )
    this.credentialSource = options.credentialSource
    if (this.credentialSource) {
      this.auth = null
    } else {
      const storage = options.storage ?? window.localStorage
      const vault = BrowserCredentialVault.forReference(profile.credentialRef, storage)
      this.auth = new ConnectionManager(vault, new PairingTransport(this.fetcher))
    }
    this.state = {
      instanceId: this.instanceId,
      status: 'connecting',
      authStatus: 'restoring',
      realtime: null,
      reconnectAttempt: 0,
    }
    this.engineOptions = {
      scheduler: this.scheduler,
      socketFactory: options.socketFactory,
      reconnectBaseMs: this.reconnectBaseMs,
      reconnectMaxMs: this.reconnectMaxMs,
      reconnectJitter: (delayMs) => jitterDelay(delayMs, this.random),
    }
  }

  getState(): FleetInstanceRuntimeState {
    return this.state
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener)
    listener(this.state)
    return () => this.listeners.delete(listener)
  }

  async start(): Promise<void> {
    if (this.running) return
    this.running = true
    await this.authenticateAndStart()
  }

  stop(): void {
    if (!this.running && this.state.status === 'offline') return
    this.running = false
    this.generation += 1
    this.clearAuthRetry()
    this.teardownEngine()
    this.setState({
      ...this.state,
      status: 'offline',
      reconnectAttempt: 0,
      lastError: undefined,
    })
  }

  async activity(options: FleetActivityOptions = {}): Promise<ActivityFeedReadModel> {
    if (!this.client || this.state.authStatus !== 'connected') {
      throw new Error('instance_not_connected:' + this.instanceId)
    }
    return this.client.activity(options)
  }

  async task(namespace: string, taskId: string): Promise<TaskReadModel> {
    if (!this.client || this.state.authStatus !== 'connected') {
      throw new Error('instance_not_connected:' + this.instanceId)
    }
    return this.client.task(namespace, taskId)
  }

  async persistentMutation(path: string, body: Record<string, unknown>): Promise<PersistentMutationResult> {
    // Realtime/read freshness and authenticated HTTP write reachability are separate planes.
    // Keep mutations available while the authenticated client exists; authority validation
    // decides whether the revisioned operation is legal.
    if (!this.client || this.state.authStatus !== 'connected') {
      throw new Error('instance_not_connected:' + this.instanceId)
    }
    const result = await this.client.persistentMutation(path, body)
    this.engine?.applyPersistentMutation(result)
    return result
  }

  async retryNow(): Promise<void> {
    if (!this.running) return this.start()
    this.clearAuthRetry()
    this.authAttempt = 0
    const realtime = this.state.realtime
    if (
      this.engine &&
      realtime &&
      realtime.status !== 'offline' &&
      this.state.authStatus === 'connected'
    ) {
      return this.engine.retryNow()
    }
    this.teardownEngine()
    await this.authenticateAndStart()
  }

  private async authenticateAndStart(): Promise<void> {
    if (!this.running) return
    const generation = ++this.generation
    this.clearAuthRetry()
    this.setState({
      ...this.state,
      status: 'connecting',
      authStatus: 'restoring',
      reconnectAttempt: this.authAttempt,
      lastError: undefined,
    })

    const authState = await this.restoreAccess()
    if (!this.running || generation !== this.generation) return

    if (authState.status !== 'connected') {
      if (authState.status === 'error' && authState.retryable) {
        this.scheduleAuthRetry(authState.message, 'error')
        return
      }
      this.setState({
        ...this.state,
        status: 'offline',
        authStatus: authState.status,
        reconnectAttempt: this.authAttempt,
        lastError:
          authState.status === 'error' ? authState.message : 'auth_' + authState.status,
      })
      return
    }

    this.authAttempt = 0
    this.accessToken = authState.accessToken
    const client = new ConsoleClient(
      this.profile.origin,
      () => this.accessToken,
      this.fetcher,
    )
    this.client = client
    const engine = new RealtimeConsoleEngine(client, this.profile.origin, this.engineOptions)
    this.engine = engine
    await engine.start()
    if (!this.running || generation !== this.generation) {
      engine.stop()
      return
    }

    this.engineUnsubscribe = engine.subscribe((realtimeState) => {
      if (!this.running || this.engine !== engine) return
      this.setState({
        instanceId: this.instanceId,
        status: realtimeState.status,
        authStatus: 'connected',
        realtime: realtimeState,
        reconnectAttempt: Math.max(this.authAttempt, realtimeState.reconnectAttempt),
        lastError: realtimeState.lastError,
      })
      if (
        realtimeState.status === 'offline' &&
        ['unauthorized', 'revoked_device'].includes(realtimeState.lastError ?? '')
      ) {
        this.credentialSource?.invalidateAccessSession?.(this.instanceId)
        this.scheduleAuthRetry(realtimeState.lastError)
      }
    })
  }


  private async restoreAccess(): Promise<
    | { status: 'connected'; accessToken: string }
    | { status: 'unpaired' | 'revoked' | 'expired' | 'disconnected' | 'pairing' | 'restoring' }
    | { status: 'error'; retryable: boolean; message: string }
  > {
    if (this.credentialSource) {
      const result = await this.credentialSource.restore(this.instanceId)
      switch (result.status) {
        case 'connected':
          return { status: 'connected', accessToken: result.accessToken }
        case 'missing':
        case 'unpaired':
          return { status: 'unpaired' }
        case 'revoked':
        case 'expired':
          return { status: result.status }
        case 'error':
          return { status: 'error', retryable: result.retryable, message: result.message }
      }
    }

    const result = await this.auth!.restore()
    switch (result.status) {
      case 'connected':
        return { status: 'connected', accessToken: result.session.accessToken }
      case 'error':
        return { status: 'error', retryable: result.retryable, message: result.message }
      default:
        return { status: result.status }
    }
  }

  private scheduleAuthRetry(
    error?: string,
    authStatus: FleetInstanceRuntimeState['authStatus'] = this.state.authStatus,
  ): void {
    if (!this.running || this.authRetryTimer !== null) return
    this.teardownEngine()
    const attempt = ++this.authAttempt
    const bounded = boundedBackoffDelay(
      attempt - 1,
      this.reconnectBaseMs,
      this.reconnectMaxMs,
    )
    const delay = Math.min(this.reconnectMaxMs, jitterDelay(bounded, this.random))
    this.setState({
      ...this.state,
      status: 'reconnecting',
      authStatus,
      reconnectAttempt: attempt,
      lastError: error,
    })
    this.authRetryTimer = this.scheduler.setTimeout(() => {
      this.authRetryTimer = null
      void this.authenticateAndStart()
    }, delay)
  }

  private teardownEngine(): void {
    this.engineUnsubscribe?.()
    this.engineUnsubscribe = null
    this.client = null
    const engine = this.engine
    this.engine = null
    engine?.stop()
  }

  private clearAuthRetry(): void {
    if (this.authRetryTimer === null) return
    this.scheduler.clearTimeout(this.authRetryTimer)
    this.authRetryTimer = null
  }

  private setState(state: FleetInstanceRuntimeState): void {
    this.state = state
    for (const listener of this.listeners) listener(state)
  }
}

export function browserFleetActorFactory(
  options: BrowserFleetActorOptions = {},
): (profile: ConnectionProfile) => BrowserFleetInstanceActor {
  return (profile) => new BrowserFleetInstanceActor(profile, options)
}
