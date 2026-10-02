import { ConsoleHttpError, type FetchLike } from '../api/client'
import type { ConnectionProfile, ProfileRestoreResult } from '../connections/types'
import type { FleetIngressEndpoint } from './adaptiveRuntime'
import type { FleetIngressProbe } from './selector'
import type { FleetQueryRequest } from './v1Types'
import { FleetV1Client } from './v1Client'

const FLEET_PROTOCOL_MAJOR = 1
const REQUIRED_CAPABILITY = 'fleet.projection.current-v2'
const TOKEN_REFRESH_SKEW_MS = 30_000

export type FleetCredentialSource = {
  restore(instanceId: string): Promise<ProfileRestoreResult>
  invalidateAccessSession?(instanceId: string): void
}

export class BrowserFleetIngressEndpoint implements FleetIngressEndpoint {
  readonly candidateId: string
  readonly profile: ConnectionProfile
  private readonly client: FleetV1Client
  private accessToken = ''
  private accessExpiresAt = 0
  private attempts = 0
  private failures = 0
  private _nodeId?: string
  private _fleetId?: string

  constructor(
    profile: ConnectionProfile,
    private readonly credentials: FleetCredentialSource,
    fetcher: FetchLike = fetch,
    private readonly now: () => number = Date.now,
  ) {
    this.profile = profile
    this.candidateId = profile.instanceId
    this.client = new FleetV1Client(profile.origin, () => this.accessToken, fetcher)
  }

  get nodeId(): string | undefined { return this._nodeId }
  get fleetId(): string | undefined { return this._fleetId }

  async probe(): Promise<FleetIngressProbe> {
    const started = this.now()
    this.attempts += 1
    try {
      const result = await this.authorized(() => this.client.probe())
      this._nodeId = result.nodeId
      this._fleetId = result.fleetId
      const sources = result.sources
      const completeness = sources.length === 0 ? 1 :
        sources.filter((item) => item.freshness !== 'reset_required' && item.freshness !== 'unavailable').length / sources.length
      const freshness = sources.length === 0 ? 1 :
        sources.filter((item) => item.freshness === 'fresh').length / sources.length
      return {
        candidateId: this.candidateId,
        authenticated: true,
        compatible: result.protocolMajor === FLEET_PROTOCOL_MAJOR &&
          result.capabilities.includes(REQUIRED_CAPABILITY),
        fleetId: result.fleetId,
        projectionEpoch: result.projectionEpoch,
        projectionSeq: result.projectionSeq,
        rttMs: Math.max(0, this.now() - started),
        successRate: (this.attempts - this.failures) / this.attempts,
        reconnectRate: this.failures / this.attempts,
        completeness,
        freshness,
      }
    } catch (error) {
      this.failures += 1
      throw error
    }
  }

  async snapshot() {
    const snapshot = await this.authorized(() => this.client.snapshot())
    if (this._fleetId && snapshot.fleetId !== this._fleetId) throw new Error('fleet_identity_changed')
    return snapshot
  }

  async events(since: number, limit?: number) {
    return this.authorized(() => this.client.events(since, limit))
  }

  async query<T = Record<string, unknown>>(resource: string, input: FleetQueryRequest = {}) {
    return this.authorized(() => this.client.query<T>(resource, input))
  }

  async detail<T = Record<string, unknown>>(resource: string, entityId: string, sourceNodeId?: string) {
    return this.authorized(() => this.client.detail<T>(resource, entityId, sourceNodeId))
  }

  async namespaces(input: Omit<FleetQueryRequest, 'filters'> = {}) {
    return this.authorized(() => this.client.namespaces(input))
  }

  async taskGraph(namespace: string, taskId: string, depth = 2, sourceNodeId?: string) {
    return this.authorized(() => this.client.taskGraph(namespace, taskId, depth, sourceNodeId))
  }

  private async authorized<T>(operation: () => Promise<T>): Promise<T> {
    await this.ensureAccess()
    try {
      return await operation()
    } catch (error) {
      if (
        !(error instanceof ConsoleHttpError)
        || !error.authenticationRequired
        || !this.credentials.invalidateAccessSession
      ) throw error
      this.credentials.invalidateAccessSession(this.profile.instanceId)
      this.accessToken = ''
      this.accessExpiresAt = 0
      await this.ensureAccess()
      return operation()
    }
  }

  private async ensureAccess(): Promise<void> {
    if (this.accessToken && this.accessExpiresAt > this.now() + TOKEN_REFRESH_SKEW_MS) return
    const restored = await this.credentials.restore(this.profile.instanceId)
    if (restored.status !== 'connected') throw new Error('fleet_ingress_auth_' + restored.status)
    if (restored.profile.origin !== this.profile.origin) throw new Error('fleet_ingress_profile_origin_mismatch')
    this.accessToken = restored.accessToken
    this.accessExpiresAt = restored.accessExpiresAt
  }
}

export function browserFleetIngressEndpoints(
  profiles: ConnectionProfile[],
  credentials: FleetCredentialSource,
  fetcher: FetchLike = fetch,
  now: () => number = Date.now,
): BrowserFleetIngressEndpoint[] {
  return profiles.map((profile) => new BrowserFleetIngressEndpoint(profile, credentials, fetcher, now))
}
