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
} from './v1Types'

export type FleetIngressEndpoint = {
  candidateId: string
  probe(): Promise<FleetIngressProbe>
  snapshot(): Promise<FleetProjectionSnapshot>
  events(since: number, limit?: number): Promise<FleetProjectionEventPage>
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
  cache: FleetCacheView
}

type Listener = (state: FleetAdaptiveRuntimeState) => void

export class FleetAdaptiveReadRuntime {
  private readonly listeners = new Set<Listener>()
  private readonly endpoints = new Map<string, FleetIngressEndpoint>()
  private state!: FleetAdaptiveRuntimeState
  private active: FleetIngressEndpoint | null = null

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
    const cached = await this.cache.restore()
    try {
      const page = await this.active.events(cached.appliedProjectionSeq)
      if (
        page.resetRequired ||
        page.projectionEpoch !== cached.projectionEpoch
      ) {
        await this.replaceFromSnapshot(this.active)
      } else {
        await this.cache.applyEvents(page)
      }
      this.setState(this.view('live', await this.cache.restore()))
    } catch (error) {
      this.selector.transientFailure(this.active.candidateId, this.now())
      this.setState(this.view('degraded', await this.cache.restore(), errorCode(error)))
    }
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
      await this.replaceFromSnapshot(endpoint)
    } else {
      const cached = await this.cache.restore()
      const page = await endpoint.events(cached.appliedProjectionSeq)
      if (page.resetRequired || page.projectionEpoch !== cached.projectionEpoch) {
        await this.replaceFromSnapshot(endpoint)
      } else {
        await this.cache.applyEvents(page)
      }
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
