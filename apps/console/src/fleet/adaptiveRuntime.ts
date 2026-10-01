import type { FleetProjectionCache } from './cache'
import {
  FleetIngressSelector,
  type FleetHandoverDecision,
  type FleetIngressProbe,
  type FleetIngressState,
} from './selector'
import type {
  FleetCacheView,
  FleetProjectionEventPage,
  FleetProjectionSnapshot,
  FleetQueryRequest,
  FleetRelayResult,
} from './v1Types'

export type FleetIngressEndpoint = {
  candidateId: string
  probe(): Promise<FleetIngressProbe>
  snapshot(): Promise<FleetProjectionSnapshot>
  events(since: number, limit?: number): Promise<FleetProjectionEventPage>
  query?<T = Record<string, unknown>>(
    resource: string,
    input?: FleetQueryRequest,
  ): Promise<FleetRelayResult<T>>
  detail?<T = Record<string, unknown>>(
    resource: string,
    entityId: string,
    sourceNodeId?: string,
  ): Promise<FleetRelayResult<T>>
  namespaces?(input?: Omit<FleetQueryRequest, 'filters'>): Promise<FleetRelayResult<Record<string, unknown>>>
  taskGraph?(
    namespace: string,
    taskId: string,
    depth?: number,
    sourceNodeId?: string,
  ): Promise<FleetRelayResult<Record<string, unknown>>>
}

export type FleetAdaptiveRuntimeState = {
  status: 'dormant' | 'restoring' | 'live' | 'degraded' | 'fallback'
  selectorState: FleetIngressState
  activeIngressId?: string
  preferredIngressId?: string
  fleetId?: string
  projectionEpoch: number
  projectionSeq: number
  networkEpoch: number
  lastSwitchReason?: FleetHandoverDecision['reason']
  lastError?: string
  catchupWarning?: string
  cache: FleetCacheView
}

type Listener = (state: FleetAdaptiveRuntimeState) => void

export class FleetAdaptiveReadRuntime {
  private readonly listeners = new Set<Listener>()
  private readonly endpoints = new Map<string, FleetIngressEndpoint>()
  private state!: FleetAdaptiveRuntimeState
  private active: FleetIngressEndpoint | null = null
  private recovery: Promise<void> | null = null
  private readonly queryFlights = new Map<string, Promise<unknown>>()
  private catchupWarning?: string

  constructor(
    private readonly cache: FleetProjectionCache,
    private readonly selector = new FleetIngressSelector(),
    private readonly now: () => number = Date.now,
  ) {}

  getState(): FleetAdaptiveRuntimeState {
    return this.state
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener)
    if (this.state) listener(this.state)
    return () => this.listeners.delete(listener)
  }

  async start(endpoints: FleetIngressEndpoint[]): Promise<boolean> {
    this.endpoints.clear()
    for (const endpoint of endpoints) this.endpoints.set(endpoint.candidateId, endpoint)
    const cached = await this.cache.restore()
    this.selector.restorePreferred(cached.preferredIngressId)
    this.setState(this.view('restoring', cached))

    const ordered = [...endpoints].sort((a, b) => {
      if (a.candidateId === cached.preferredIngressId) return -1
      if (b.candidateId === cached.preferredIngressId) return 1
      return a.candidateId.localeCompare(b.candidateId)
    })
    for (const endpoint of ordered) {
      try {
        const probe = await endpoint.probe()
        if (cached.fleetId && probe.fleetId !== cached.fleetId) continue
        const decision = this.selector.observeProbe(probe, this.now())
        if (!decision) continue
        await this.applyHandover(endpoint, decision)
        return true
      } catch {
        this.selector.transientFailure(endpoint.candidateId, this.now())
      }
    }
    this.selector.noGateway()
    this.active = null
    this.setState(this.view('fallback', await this.cache.restore(), 'no_compatible_ingress'))
    return false
  }

  async evaluate(endpoints = [...this.endpoints.values()]): Promise<void> {
    for (const endpoint of endpoints) {
      try {
        const probe = await endpoint.probe()
        if (this.state.fleetId && probe.fleetId !== this.state.fleetId) continue
        const decision = this.selector.observeProbe(probe, this.now())
        if (decision) {
          await this.applyHandover(endpoint, decision)
          return
        }
      } catch {
        if (endpoint.candidateId === this.active?.candidateId) {
          this.selector.transientFailure(endpoint.candidateId, this.now())
        }
      }
    }
    this.setState(this.view(this.active ? 'live' : 'degraded', await this.cache.restore()))
  }

  async syncOnce(): Promise<void> {
    if (!this.active) return
    try {
      await this.catchUp(this.active)
      this.setState(this.view('live', await this.cache.restore()))
    } catch (error) {
      this.selector.transientFailure(this.active.candidateId, this.now())
      this.setState(this.view('degraded', await this.cache.restore(), errorCode(error)))
    }
  }

  query<T = Record<string, unknown>>(
    resource: string,
    input: FleetQueryRequest = {},
  ): Promise<FleetRelayResult<T>> {
    return this.singleFlight(
      'query:' + resource + ':' + stableKey(input),
      (endpoint) => {
        if (!endpoint.query) throw new Error('fleet_query_capability_unavailable')
        return endpoint.query<T>(resource, input)
      },
    )
  }

  detail<T = Record<string, unknown>>(
    resource: string,
    entityId: string,
    sourceNodeId?: string,
  ): Promise<FleetRelayResult<T>> {
    return this.singleFlight(
      'detail:' + resource + ':' + entityId + ':' + (sourceNodeId ?? ''),
      (endpoint) => {
        if (!endpoint.detail) throw new Error('fleet_query_capability_unavailable')
        return endpoint.detail<T>(resource, entityId, sourceNodeId)
      },
    )
  }

  namespaces(
    input: Omit<FleetQueryRequest, 'filters'> = {},
  ): Promise<FleetRelayResult<Record<string, unknown>>> {
    return this.singleFlight(
      'namespaces:' + stableKey(input),
      (endpoint) => {
        if (!endpoint.namespaces) throw new Error('fleet_query_capability_unavailable')
        return endpoint.namespaces(input)
      },
    )
  }

  taskGraph(
    namespace: string,
    taskId: string,
    depth = 2,
    sourceNodeId?: string,
  ): Promise<FleetRelayResult<Record<string, unknown>>> {
    return this.singleFlight(
      ['graph', namespace, taskId, depth, sourceNodeId ?? ''].join(':'),
      (endpoint) => {
        if (!endpoint.taskGraph) throw new Error('fleet_query_capability_unavailable')
        return endpoint.taskGraph(namespace, taskId, depth, sourceNodeId)
      },
    )
  }

  async hardFailActive(): Promise<boolean> {
    if (!this.active) return false
    this.selector.hardFailure(this.active.candidateId, this.now())
    for (const endpoint of this.endpoints.values()) {
      if (endpoint.candidateId === this.active.candidateId) continue
      try {
        const probe = await endpoint.probe()
        if (this.state.fleetId && probe.fleetId !== this.state.fleetId) continue
        const decision = this.selector.observeProbe(probe, this.now())
        if (!decision) continue
        await this.applyHandover(endpoint, decision)
        return true
      } catch {
        // A failed alternative handshake is not a viable failover target.
      }
    }
    this.setState(this.view('degraded', await this.cache.restore(), 'no_failover_ingress'))
    return false
  }

  networkChanged(): void {
    this.selector.networkChanged()
    if (this.state) this.setState({ ...this.state, networkEpoch: this.selector.getNetworkEpoch() })
  }

  setBackground(value: boolean): void {
    this.selector.setBackground(value)
  }

  private async applyHandover(
    endpoint: FleetIngressEndpoint,
    decision: FleetHandoverDecision,
  ): Promise<void> {
    if (decision.requiresSnapshot) {
      await this.recover(endpoint)
    } else {
      await this.catchUp(endpoint)
    }

    const cached = await this.cache.restore()
    const snapshotFleet = cached.fleetId
    if (!snapshotFleet) throw new Error('fleet_identity_missing')
    const previousFleet = this.state?.fleetId
    if (previousFleet && previousFleet !== snapshotFleet) throw new Error('fleet_identity_mismatch')

    await this.cache.setPreferredIngress(endpoint.candidateId)
    this.selector.commitHandover(decision, this.now())
    this.active = endpoint
    this.setState(
      this.view('live', await this.cache.restore(), undefined, decision.reason),
    )
  }

  private async catchUp(endpoint: FleetIngressEndpoint): Promise<void> {
    const startedAt = this.now()
    this.catchupWarning = undefined
    for (let pageNumber = 0; pageNumber < 100; pageNumber += 1) {
      const cached = await this.cache.restore()
      const before = cached.appliedProjectionSeq
      const page = await endpoint.events(before)
      if (page.resetRequired || page.projectionEpoch !== cached.projectionEpoch) {
        await this.recover(endpoint)
        return
      }
      if (page.events.length > 0) await this.cache.applyEvents(page)
      const after = (await this.cache.restore()).appliedProjectionSeq
      const elapsed = Math.max(0, this.now() - startedAt)
      if (elapsed > 10_000) throw new Error('fleet_projection_catchup_timeout')
      if (elapsed > 5_000) this.catchupWarning = 'fleet_projection_catchup_slow'
      if (after >= page.projectionSeq) return
      if (page.events.length === 0 || after <= before) {
        throw new Error('fleet_projection_catchup_stalled')
      }
    }
    throw new Error('fleet_projection_catchup_limit')
  }

  private recover(endpoint: FleetIngressEndpoint): Promise<void> {
    if (this.recovery) return this.recovery
    const recovery = this.replaceFromSnapshot(endpoint)
      .finally(() => {
        if (this.recovery === recovery) this.recovery = null
      })
    this.recovery = recovery
    return recovery
  }

  private singleFlight<T>(
    key: string,
    operation: (endpoint: FleetIngressEndpoint) => Promise<T>,
  ): Promise<T> {
    const existing = this.queryFlights.get(key) as Promise<T> | undefined
    if (existing) return existing
    if (!this.active) return Promise.reject(new Error('fleet_query_unavailable_offline'))
    const promise = operation(this.active)
      .finally(() => {
        if (this.queryFlights.get(key) === promise) this.queryFlights.delete(key)
      })
    this.queryFlights.set(key, promise)
    return promise
  }

  private async replaceFromSnapshot(endpoint: FleetIngressEndpoint): Promise<void> {
    const snapshot = await endpoint.snapshot()
    const cached = await this.cache.restore()
    if (cached.fleetId && cached.fleetId !== snapshot.fleetId) {
      throw new Error('fleet_identity_mismatch')
    }
    await this.cache.applySnapshot(snapshot)
  }

  private view(
    status: FleetAdaptiveRuntimeState['status'],
    cache: FleetCacheView,
    lastError?: string,
    lastSwitchReason?: FleetHandoverDecision['reason'],
  ): FleetAdaptiveRuntimeState {
    return {
      status,
      selectorState: this.selector.getState(),
      activeIngressId: this.selector.getActiveCandidateId(),
      preferredIngressId: cache.preferredIngressId,
      fleetId: cache.fleetId,
      projectionEpoch: cache.projectionEpoch,
      projectionSeq: cache.appliedProjectionSeq,
      networkEpoch: this.selector.getNetworkEpoch(),
      lastSwitchReason: lastSwitchReason ?? this.state?.lastSwitchReason,
      lastError,
      catchupWarning: this.catchupWarning,
      cache,
    }
  }

  private setState(state: FleetAdaptiveRuntimeState): void {
    this.state = state
    for (const listener of this.listeners) listener(state)
  }
}

function errorCode(error: unknown): string {
  if (error instanceof Error && error.message) return error.message
  return 'fleet_runtime_error'
}


function stableKey(value: unknown): string {
  if (value === null || typeof value !== 'object') return JSON.stringify(value)
  if (Array.isArray(value)) return '[' + value.map(stableKey).join(',') + ']'
  const item = value as Record<string, unknown>
  return '{' + Object.keys(item).sort().map((key) => JSON.stringify(key) + ':' + stableKey(item[key])).join(',') + '}'
}
